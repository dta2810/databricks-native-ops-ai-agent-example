CREATE SCHEMA IF NOT EXISTS ${catalog}.${schema} COMMENT 'Beverage Ops Agent demo (fictitious Acme Beverages, synthetic data)';
CREATE VOLUME IF NOT EXISTS ${catalog}.${schema}.${volume} COMMENT 'Landing files for synthetic data and document corpus'
