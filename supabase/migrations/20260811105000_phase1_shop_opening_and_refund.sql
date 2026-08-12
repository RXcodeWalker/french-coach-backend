/*
  # Shop Phase 1 — opening balance import + deleted-SKU refund (plan §14.5)

  Pure SQL, idempotent by primary key (ON CONFLICT DO NOTHING on both
  inserts) — safe to re-run.

  Ordering matters: the opening-balance grant runs first, importing each
  user's existing profiles.gems value as their starting gem_events balance
  before gem_events existed. Refunds for the deleted Shop Phase 0 SKUs run
  second, in the same migration, so a user's final balance is always
  (pre-existing gems) + (refund), never just the refund on its own.

  Refund set — the 15 SKUs deleted from src/data/shopItems.ts in Shop Phase
  0 (frontend commit 4ffc58f), with their original per-unit prices pulled
  from that commit's parent (git show 4ffc58f~1:src/data/shopItems.ts).
  perfect_streak_repair is included per the explicit decision when Phase 0
  was scoped: it's superseded by this phase's server-seeded `streak_repair`
  item, but existing holders get refunded the 3000 gems they paid for it
  rather than silently losing the item.

  gem_events.item_id is a real FK into shop_items — none of these deleted
  ids exist there (they were client-only and are not part of the new
  19-item catalogue seeded by the next migration), so refund rows leave
  item_id NULL and carry the deleted id as audit metadata instead, the same
  pattern scoring_envelopes.regraded_from uses for a reference that isn't a
  join target.
*/

-- ── opening balance ───────────────────────────────────────────────────────

INSERT INTO gem_events (id, user_id, delta, kind, metadata)
SELECT
  'opening:v1:' || id,
  id,
  gems,
  'grant',
  jsonb_build_object('reason', 'opening_balance_import')
FROM profiles
WHERE gems <> 0
ON CONFLICT (id) DO NOTHING;

-- ── deleted-SKU refunds ───────────────────────────────────────────────────

INSERT INTO gem_events (id, user_id, delta, kind, item_id, metadata)
SELECT
  'refund:v1:' || p.id || ':' || d.item_id,
  p.id,
  (p.inventory->>d.item_id)::integer * d.original_price,
  'refund',
  NULL,
  jsonb_build_object(
    'reason', 'sku_deleted',
    'deleted_item_id', d.item_id,
    'qty', (p.inventory->>d.item_id)::integer,
    'original_price', d.original_price
  )
FROM profiles p
CROSS JOIN (VALUES
  ('xp_boost',              150),
  ('perfect_shield',        100),
  ('legendary_avatar',      1000),
  ('mystery_key',           500),
  ('linguist_cape',         800),
  ('time_warp',             300),
  ('gold_border',           1500),
  ('double_gems',           2000),
  ('diamond_badge',         5000),
  ('streak_shield_mega',    1200),
  ('retro_theme',           3000),
  ('fast_pass',             1500),
  ('premium_voice',         2500),
  ('flashcard_pack_idioms', 600),
  ('perfect_streak_repair', 3000)
) AS d(item_id, original_price)
WHERE p.inventory ? d.item_id
  AND (p.inventory->>d.item_id)::integer > 0
ON CONFLICT (id) DO NOTHING;
