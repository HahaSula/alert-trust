\set a random(1, 900000)
SELECT count(*) FROM (SELECT DISTINCT filler, abalance FROM pgbench_accounts WHERE aid BETWEEN :a AND :a + 100000) s;
