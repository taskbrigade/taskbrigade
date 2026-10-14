-- Remote MariaDB >= 10.5. For a database on the same Ubuntu host, use docs/quickstart.md
-- (user 'taskadmin'@'localhost', port 3306) instead of the subnet grant below.
-- Run once as DB admin (database taskbrigade and user taskadmin already exist).
-- Network 192.168.1.0/255.255.255.0 is an example; replace it with the app server's network.
ALTER DATABASE taskbrigade CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS taskbrigade_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES, LOCK TABLES
  ON taskbrigade.* TO 'taskadmin'@'192.168.1.0/255.255.255.0';
GRANT ALL PRIVILEGES ON taskbrigade_test.* TO 'taskadmin'@'192.168.1.0/255.255.255.0';
FLUSH PRIVILEGES;

-- Test from the server: mariadb -h db.example.internal -P 3307 -u taskadmin -p taskbrigade -e 'SELECT VERSION();'
