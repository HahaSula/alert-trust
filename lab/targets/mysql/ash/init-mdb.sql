-- MariaDB（mdb、gal1；gal2、gal3 由 SST 複製 gal1 的資料目錄）
CREATE USER 'exporter'@'%' IDENTIFIED BY 'exporterpw' WITH MAX_USER_CONNECTIONS 4;
GRANT PROCESS, REPLICATION CLIENT, SLAVE MONITOR, SELECT ON *.* TO 'exporter'@'%';
CREATE DATABASE app;
CREATE TABLE app.hot (id INT PRIMARY KEY, v INT NOT NULL DEFAULT 0);
INSERT INTO app.hot VALUES (1,0),(2,0),(3,0);
CREATE TABLE app.d (n INT PRIMARY KEY);
INSERT INTO app.d VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);
CREATE TABLE app.big (id INT PRIMARY KEY, k INT NOT NULL, pad CHAR(100) NOT NULL);
INSERT INTO app.big SELECT a.n + 10*b.n + 100*c.n + 1000*d.n + 1, FLOOR(RAND()*1000), MD5(RAND())
  FROM app.d a, app.d b, app.d c, app.d d;
-- capacity：TINYINT 自動遞增（上限 127）
CREATE TABLE app.ai (id TINYINT AUTO_INCREMENT PRIMARY KEY, x INT);
