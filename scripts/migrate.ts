import 'dotenv/config';
import { Client } from 'pg';
import { drizzle } from 'drizzle-orm/node-postgres';
import { migrate } from 'drizzle-orm/node-postgres/migrator';

async function main() {
  const url = process.env.DATABASE_URL;
  if (!url || !/^postgres(ql)?:\/\//.test(url)) throw new Error('DATABASE_URL must be a PostgreSQL secret');
  const client = new Client({ connectionString: url, connectionTimeoutMillis: 5000,
    statement_timeout: 30000, application_name: 'karry-media-migration' });
  try {
    await client.connect();
    await client.query("SET lock_timeout = '5s'");
    await client.query('SELECT pg_advisory_lock(734923412)');
    try {
      await migrate(drizzle(client), { migrationsFolder: './drizzle' });
      const result = await client.query("SELECT to_regclass('public.media_files') as table_name");
      if (!result.rows[0]?.table_name) throw new Error('media_files not found');
      console.log('PostgreSQL migration applied: media_files + media_generation_jobs');
    } finally {
      await client.query('SELECT pg_advisory_unlock(734923412)');
    }
  } finally { await client.end(); }
}

main().catch(() => {
  console.error('Migration failed. Check DATABASE_URL, connection, permissions and schema; secrets are not logged.');
  process.exitCode = 1;
});
