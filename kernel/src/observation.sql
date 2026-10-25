CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS streams(
                  node TEXT NOT NULL,user TEXT NOT NULL,stream TEXT NOT NULL,
                  seq INTEGER NOT NULL,source_generation TEXT,gap TEXT,received REAL NOT NULL,
                  PRIMARY KEY(node,user));
                CREATE TABLE IF NOT EXISTS items(
                  id TEXT PRIMARY KEY,node TEXT NOT NULL,user TEXT NOT NULL,project TEXT NOT NULL,
                  provider TEXT NOT NULL,thread TEXT NOT NULL,task TEXT,received REAL NOT NULL,
                  ordinal INTEGER NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS observation_list ON items(user,id);
                CREATE INDEX IF NOT EXISTS observation_recent ON items(user,ordinal DESC,id);
                CREATE INDEX IF NOT EXISTS observation_identity ON items(user,provider,thread);
                CREATE TABLE IF NOT EXISTS turns(
                  observation TEXT NOT NULL,turn_key TEXT NOT NULL,ordinal INTEGER NOT NULL,
                  received REAL NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(observation,turn_key));
                CREATE INDEX IF NOT EXISTS observation_turns ON turns(observation,ordinal DESC,turn_key);
                CREATE TABLE IF NOT EXISTS events(
                  node TEXT NOT NULL,user TEXT NOT NULL,stream TEXT NOT NULL,seq INTEGER NOT NULL,
                  digest TEXT NOT NULL,received REAL NOT NULL,PRIMARY KEY(node,user,stream,seq));
