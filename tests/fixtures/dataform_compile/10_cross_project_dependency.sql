CREATE OR REPLACE TABLE `project-a`.smoke.project_target AS SELECT 1 AS id;
UPDATE `project-a`.smoke.project_target SET id = 2 WHERE TRUE;
CREATE OR REPLACE TABLE smoke.cross_project_reader AS
SELECT * FROM `project-a`.smoke.project_target;
CREATE OR REPLACE TABLE `project-b`.smoke.project_target AS SELECT 3 AS id;

