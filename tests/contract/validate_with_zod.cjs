// Validasi fixture kontrak MCP dengan schema Zod ASLI Dashboard (build dist/).
//   node tests/contract/validate_with_zod.cjs <dashboard_backend_dir> <fixtures_dir>
// Output: JSON [{name, valid, issues}] ke stdout. Port test ini ke repo Dashboard sebagai
// backend/test/contract/mcp-fixtures.spec.ts (lihat documentation/dashboard-pr/).
const fs = require('node:fs');
const path = require('node:path');

const [backendDir, fixturesDir] = process.argv.slice(2).map((p) => path.resolve(p));
const dist = path.join(backendDir, 'dist', 'shared', 'schemas');
const mcp = require(path.join(dist, 'mcp.schema.js'));
const blocks = require(path.join(dist, 'block.schema.js'));

const outputSchemas = {
  dispatch: mcp.dispatchOutputSchema,
  estimation: mcp.estimationOutputSchema,
  cancel: mcp.cancelOutputSchema,
};

function issuesOf(result) {
  return result.success ? [] : result.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`);
}

function checkEvent(payload) {
  const env = mcp.webhookEnvelopeSchema.safeParse(payload);
  if (!env.success) return issuesOf(env);
  const data = mcp.eventDataSchemas[env.data.event_type].safeParse(env.data.data);
  if (!data.success) return issuesOf(data);
  // Dashboard memvalidasi content_json per blok saat menerapkan operasi
  const issues = [];
  const checkBlock = (blockType, content) => {
    if (blockType === undefined) return;
    const r = blocks.validateBlockContent(blockType, content);
    if (!r.ok) issues.push(...r.issues.map((i) => `${i.path}: ${i.message}`));
  };
  for (const op of data.data.operations ?? []) checkBlock(op.block.block_type, op.block.content_json);
  for (const s of data.data.subtasks ?? []) for (const b of s.initial_blocks ?? []) checkBlock(b.block_type, b.content_json);
  return issues;
}

const results = [];
for (const file of fs.readdirSync(fixturesDir).filter((f) => f.endsWith('.json')).sort()) {
  const fx = JSON.parse(fs.readFileSync(path.join(fixturesDir, file), 'utf8'));
  const issues = fx.kind === 'event' ? checkEvent(fx.payload) : issuesOf(outputSchemas[fx.schema].safeParse(fx.payload));
  results.push({ name: fx.name, expected: fx.valid, valid: issues.length === 0, issues });
}
process.stdout.write(JSON.stringify(results));
