CREATE USER 'exporter'@'%' IDENTIFIED BY 'exporterpw' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
CREATE USER 'app'@'%' IDENTIFIED BY 'apppw';
GRANT ALL ON app.* TO 'app'@'%';
CREATE DATABASE app;
-- 20 萬列、約 50 MB：比 buffer pool 縮小後（16M）大，比預設 128M 小
CREATE TABLE app.sb (id INT PRIMARY KEY, k INT NOT NULL DEFAULT 0, c CHAR(120) NOT NULL, pad CHAR(60) NOT NULL, KEY (k));
CREATE TABLE app.d (n INT PRIMARY KEY);
INSERT INTO app.d VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);
INSERT INTO app.sb (id, k, c, pad)
  SELECT a.n + 10*b.n + 100*c.n + 1000*d.n + 10000*e.n + 100000*f.n + 1,
         FLOOR(RAND()*1000), MD5(RAND()), MD5(RAND())
  FROM app.d a, app.d b, app.d c, app.d d, app.d e, (SELECT n FROM app.d WHERE n < 2) f;
