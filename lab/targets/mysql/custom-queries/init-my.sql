SET SESSION sql_log_bin = 0;
CREATE USER 'exporter'@'%' IDENTIFIED BY 'exporterpw' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
-- query-exporter（queries/query-exporter/mysql.yaml 的權限）
CREATE USER 'qexporter'@'%' IDENTIFIED BY 'qepw' WITH MAX_USER_CONNECTIONS 2;
GRANT PROCESS ON *.* TO 'qexporter'@'%';
GRANT SELECT ON performance_schema.* TO 'qexporter'@'%';
GRANT SERVICE_CONNECTION_ADMIN ON *.* TO 'qexporter'@'%';
CREATE USER 'repl'@'%' IDENTIFIED BY 'replpw';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
CREATE USER 'app'@'%' IDENTIFIED BY 'apppw';
GRANT ALL ON app.* TO 'app'@'%';
