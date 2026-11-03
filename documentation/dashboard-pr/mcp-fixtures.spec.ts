// Usulan PR ke repo marketingdashboardjtp: backend/test/contract/mcp-fixtures.spec.ts
// Salin juga folder fixture MCP (tests/contract/fixtures/*.json) ke backend/test/contract/mcp-fixtures/.
// Memastikan payload yang dihasilkan MCP Marketing lolos schema Zod Dashboard (dan yang invalid ditolak).
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { validateBlockContent } from '../../src/shared/schemas/block.schema';
import {
  cancelOutputSchema,
  dispatchOutputSchema,
  estimationOutputSchema,
  eventDataSchemas,
  webhookEnvelopeSchema,
} from '../../src/shared/schemas/mcp.schema';

interface Fixture {
  name: string;
  kind: 'event' | 'output';
  schema?: 'dispatch' | 'estimation' | 'cancel';
  valid: boolean;
  payload: Record<string, unknown>;
}

const dir = join(__dirname, 'mcp-fixtures');
const fixtures: Fixture[] = readdirSync(dir)
  .filter((f) => f.endsWith('.json'))
  .map((f) => JSON.parse(readFileSync(join(dir, f), 'utf8')) as Fixture);

function eventIsValid(payload: unknown): boolean {
  const env = webhookEnvelopeSchema.safeParse(payload);
  if (!env.success) return false;
  const data = eventDataSchemas[env.data.event_type].safeParse(env.data.data);
  if (!data.success) return false;
  const d = data.data as { operations?: { block: { block_type?: string; content_json: unknown } }[]; subtasks?: { initial_blocks?: { block_type: string; content_json: unknown }[] }[] };
  const blocks = [
    ...(d.operations ?? []).map((o) => o.block),
    ...(d.subtasks ?? []).flatMap((s) => s.initial_blocks ?? []),
  ];
  return blocks.every((b) => b.block_type === undefined || validateBlockContent(b.block_type, b.content_json).ok);
}

const outputs = { dispatch: dispatchOutputSchema, estimation: estimationOutputSchema, cancel: cancelOutputSchema };

describe('MCP Marketing contract fixtures', () => {
  it.each(fixtures.map((f) => [f.name, f] as const))('%s', (_name, fx) => {
    const ok = fx.kind === 'event' ? eventIsValid(fx.payload) : outputs[fx.schema!].safeParse(fx.payload).success;
    expect(ok).toBe(fx.valid);
  });
});
