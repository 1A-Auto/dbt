import os

import dbt.targets

import psycopg2
import logging
import time
import datetime


QUERY_VALIDATE_NOT_NULL = """
with validation as (
  select {field} as f
  from "{schema}"."{table}"
)
select count(*) from validation where f is null
"""

QUERY_VALIDATE_UNIQUE = """
with validation as (
  select {field} as f
  from "{schema}"."{table}"
),
validation_errors as (
    select f from validation group by f having count(*) > 1
)
select count(*) from validation_errors
"""

QUERY_VALIDATE_ACCEPTED_VALUES = """
with all_values as (
  select distinct {field} as f
  from "{schema}"."{table}"
),
validation_errors as (
    select f from all_values where f not in ({values_csv})
)
select count(*) from validation_errors
"""

QUERY_VALIDATE_REFERENTIAL_INTEGRITY = """
with parent as (
  select {parent_field} as id
  from "{schema}"."{parent_table}"
), child as (
  select {child_field} as id
  from "{schema}"."{child_table}"
)
select count(*) from child
where id not in (select id from parent) and id is not null
"""

DDL_TEST_RESULT_CREATE = """
create table if not exists {schema}.dbt_test_results (
    tested_at timestamp without time zone,
    model_name text,
    errored bool,
    skipped bool,
    failed bool,
    count_failures integer,
    execution_time double precision
);
"""

INSERT_TEST_RESULT_TEMPLATE = """
insert into {schema}.dbt_test_results
    (tested_at, model_name, errored, skipped, failed, count_failures, execution_time)
values
    {values}
"""


class SchemaTester(object):
    def __init__(self, project):
        self.logger = logging.getLogger(__name__)
        self.project = project

        self.test_started_at = datetime.datetime.now()

    def get_target(self):
        target_cfg = self.project.run_environment()
        return dbt.targets.get_target(target_cfg)

    def execute_query(self, model, sql):
        target = self.get_target()

        with target.get_handle() as handle:
            with handle.cursor() as cursor:
                try:
                    self.logger.debug("SQL: %s", sql)
                    pre = time.time()
                    cursor.execute(sql)
                    post = time.time()
                    self.logger.debug("SQL status: %s in %d seconds", cursor.statusmessage, post-pre)
                except psycopg2.ProgrammingError as e:
                    self.logger.exception('programming error: %s', sql)
                    return e.diag.message_primary
                except Exception as e:
                    self.logger.exception('encountered exception while running: %s', sql)
                    e.model = model
                    raise e

                result = cursor.fetchone()
                if len(result) != 1:
                    self.logger.error("SQL: %s", sql)
                    self.logger.error("RESULT: %s", result)
                    raise RuntimeError("Unexpected validation result. Expected 1 record, got {}".format(len(result)))
                else:
                    return result[0]

    def validate_schema(self, schema_test):
            sql = schema_test.render()
            num_rows = self.execute_query(model, sql)
            if num_rows == 0:
                print("  OK")
                yield True
            else:
                print("  FAILED ({})".format(num_rows))
                yield False

    def create_test_results_table_if_not_exist(self):
        target = self.get_target()

        with target.get_handle() as handle:
            with handle.cursor() as cursor:
                stmt = DDL_TEST_RESULT_CREATE.format(schema=target.schema)
                try:
                    cursor.execute(stmt)
                except psycopg2.ProgrammingError as e:
                    self.logger.exception('programming error: %s', stmt)

    def insert_test_results(self, run_model_results):
        target = self.get_target()
        self.create_test_results_table_if_not_exist()

        value_template = "('{tested_at}', '{model_name}', {errored}, {skipped}, {failed}, {count_failures}, {execution_time})"

        values = []
        for res in run_model_results:
            failed_rows = 0 if res.status == "ERROR" else res.status

            value = value_template.format(
                tested_at = self.test_started_at,
                model_name = res.model.name,
                errored = "true" if res.errored else "false",
                skipped = "true" if res.skipped else "false",
                failed = "true" if failed_rows > 0 else "false",
                count_failures = failed_rows,
                execution_time = res.execution_time
            )
            values.append(value)

        joined = ",\n".join(values)
        stmt = INSERT_TEST_RESULT_TEMPLATE.format(values=joined, schema=target.schema)

        with target.get_handle() as handle:
            with handle.cursor() as cursor:
                try:
                    cursor.execute(stmt)
                except psycopg2.ProgrammingError as e:
                    self.logger.exception('programming error: %s', stmt)

