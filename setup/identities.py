"""Account-level groups for the agent (needs an account admin profile, BEV_ACCOUNT_PROFILE).

Creates three groups suffixed with BEV_ENV, assigns them to the workspace and adds members:
  planner  (read + propose)          <- BEV_PLANNERS  (comma-separated emails)
  approver (read + propose + decide) <- BEV_APPROVERS
  viewer   (read traces / MLflow)    <- BEV_VIEWERS
Planner and approver names are also the Lakebase role names (setup/52_lakebase_access.py).
Account groups are shared across workspaces: a same-name group with members outside these lists is refused (--adopt to reuse).
Idempotent; adds members, never removes them. Default = dry run.
Usage: python setup/identities.py [--apply] [--adopt]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402


def emails(var):
    return C.emails(var)  # validated + lowercased: the values reach SCIM filters


def plan():
    """{group name: [member emails]} for this environment."""
    return {C.PLANNER_GROUP: emails("BEV_PLANNERS"), C.APPROVER_GROUP: emails("BEV_APPROVERS"), C.VIEWER_GROUP: emails("BEV_VIEWERS")}


def foreign_members(have_ids, wanted_ids):
    return set(have_ids) - set(wanted_ids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--adopt", action="store_true", help="reuse a same-name group that has other members (normally refused)")
    a = ap.parse_args()
    if not getattr(C, "ACCOUNT_PROFILE", "") or not getattr(C, "WORKSPACE_ID", ""):
        sys.exit("set BEV_ACCOUNT_PROFILE and BEV_WORKSPACE_ID in the target file")
    from databricks.sdk import AccountClient
    from databricks.sdk.service import iam
    acct = AccountClient(profile=C.ACCOUNT_PROFILE)

    def user_id(email):
        u = next(iter(acct.users.list(filter=f'userName eq "{email}"')), None)
        if not u:
            sys.exit(f"user {email} not found in the account")
        return u.id

    for name, members in plan().items():
        ids = [user_id(m) for m in members]
        g = next((x for x in acct.groups.list(filter=f'displayName eq "{name}"') if x.display_name == name), None)
        g = acct.groups.get(g.id) if g else None
        have = {m.value for m in (g.members or [])} if g else set()
        if foreign_members(have, ids) and not a.adopt:
            sys.exit(f"group {name} already exists with {len(foreign_members(have, ids))} other member(s): "
                     "pick a unique BEV_ENV (or --adopt)")
        print(f"group {name}: {'exists' if g else 'create'}; members +{members}; workspace {C.WORKSPACE_ID} USER")
        if not a.apply:
            continue
        g = g or acct.groups.create(display_name=name)
        add = [{"value": i} for i in ids if i not in have]
        if add:
            acct.groups.patch(id=g.id, operations=[iam.Patch(op=iam.PatchOp.ADD, path="members", value=add)],
                              schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP])
        acct.workspace_assignment.update(workspace_id=int(C.WORKSPACE_ID), principal_id=int(g.id),
                                         permissions=[iam.WorkspacePermission.USER])
    if not a.apply:
        print("dry run: add --apply to write")


if __name__ == "__main__":
    main()
