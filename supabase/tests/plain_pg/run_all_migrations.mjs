// Applies every migration in ../../migrations, in file order, to an in-memory Postgres (PGlite)
// behind supabase_prelude.sql (Supabase's roles, auth schema and extensions schema), then runs
// each SQL test named on the command line. Nothing here talks to the hosted project.
//
// PGlite is not a dependency of this repo. Install it in a scratch folder and run from there:
//   cd <scratch> && npm install @electric-sql/pglite
//   node <this folder>/run_all_migrations.mjs <this folder>/security_review_2026_11_test.sql
// A test raises on a failed expectation; its last printed line must be 'ALL PASSED'.
import { createRequire } from 'node:module';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const migrations = join(here, '..', '..', 'migrations');
// Resolve PGlite from the folder the command is run in, not from this repo.
const req = createRequire(join(process.cwd(), 'noop.js'));
const load = async (id) => import(pathToFileURL(req.resolve(id)).href);
const { PGlite } = await load('@electric-sql/pglite');
const { pgcrypto } = await load('@electric-sql/pglite/contrib/pgcrypto');
const { uuid_ossp } = await load('@electric-sql/pglite/contrib/uuid_ossp');

const db = new PGlite({ extensions: { pgcrypto, uuid_ossp } });
await db.exec(readFileSync(join(here, 'supabase_prelude.sql'), 'utf8'));

const files = readdirSync(migrations).filter((f) => f.endsWith('.sql')).sort();
for (const f of files) {
  try {
    await db.exec(readFileSync(join(migrations, f), 'utf8'));
  } catch (err) {
    console.log(`migration FAILED ${f}: ${err.message}`);
    process.exit(1);
  }
}
console.log(`migrations applied: ${files.length}/${files.length}`);

for (const file of process.argv.slice(2)) {
  console.log(`\n== ${file}`);
  try {
    const results = await db.exec(readFileSync(file, 'utf8'));
    for (const r of results) for (const row of r.rows ?? []) console.log('  ' + Object.values(row).join(' | '));
  } catch (err) {
    console.log('  FAILED: ' + err.message);
    process.exitCode = 1;
  }
}
