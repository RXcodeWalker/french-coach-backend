/*
  # Shop Phase 1 — server economy schema (Shop plan §14.1, §14.2)

  shop_items is the server-side price list (plan §14.6 ownership model:
  "server = economic truth ... client = presentation/cache"). id/kind/
  price_gems/consumable/max_owned/requirement/active/sort_order match the
  plan's §14.2 schema table exactly. One addition beyond that table: `emoji`,
  nullable, populated only for kind='avatar' rows. The plan reuses the
  existing profiles.avatar_emoji column for the equipped avatar (§14.2 "profiles
  +2" lists only equipped_frame/equipped_nameplate as new columns) rather than
  adding a third equipped_avatar id-reference column, but equip_cosmetic (this
  phase, see the RPC migration) runs server-side and has no access to the
  client's shopCatalogue.ts presentation module — it needs *some* source for
  the glyph it writes into avatar_emoji. `emoji` is that source, scoped to the
  one slot type that needs a literal render value rather than an id reference.

  gem_events is the sole balance authority from this migration forward
  (plan §14.1): balance = SUM(delta). Append/read-only from the client's
  perspective — no INSERT/UPDATE/DELETE policy, matching the friendships/
  blocks precedent (20260809070000, 20260809080000): mutation is RPC-only,
  and the RPCs (added in a later migration this phase) are SECURITY DEFINER
  owned by postgres, which bypasses grants on tables it owns.

  user_inventory replaces profiles.inventory JSONB as the authority on what a
  user owns (plan §14.1 — profiles.inventory is "frozen" after this phase).
  qty >= 0 CHECK is defense in depth; the actual decrement-floor guard lives
  in consume_item.

  Grants are inline in the same migration that creates each table (project
  convention — see xp_events migration header: this has 42501'd twice before
  when grants were left to a separate migration).
*/

-- ── shop_items ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shop_items (
  id          text PRIMARY KEY,
  kind        text NOT NULL CHECK (kind = ANY (ARRAY['consumable', 'avatar', 'frame', 'nameplate'])),
  price_gems  integer NOT NULL CHECK (price_gems > 0),
  consumable  boolean NOT NULL DEFAULT false,
  max_owned   integer,
  requirement jsonb NOT NULL DEFAULT '{}',
  emoji       text,
  active      boolean NOT NULL DEFAULT true,
  sort_order  integer NOT NULL DEFAULT 0,
  CONSTRAINT shop_items_max_owned_positive CHECK (max_owned IS NULL OR max_owned > 0)
);

ALTER TABLE shop_items ENABLE ROW LEVEL SECURITY;

-- Public read of active items only; no write policy — the catalogue is
-- seeded by migration, never client-written (plan §14.1: server-side price
-- list).
CREATE POLICY "shop_items public read"
  ON shop_items FOR SELECT
  USING (active);

GRANT SELECT ON public.shop_items TO anon, authenticated, service_role;

-- ── gem_events ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gem_events (
  id          text PRIMARY KEY,
  user_id     uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  delta       integer NOT NULL CHECK (delta <> 0),
  kind        text NOT NULL CHECK (kind = ANY (ARRAY['earn', 'purchase', 'spend', 'refund', 'grant'])),
  item_id     text REFERENCES shop_items(id),
  metadata    jsonb NOT NULL DEFAULT '{}',
  occurred_at timestamptz NOT NULL DEFAULT now(),
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT gem_events_occurred_at_bounds CHECK (
    occurred_at >= created_at - interval '30 days'
    AND occurred_at <= created_at + interval '1 day'
  )
);

-- mint_gems' 450/day cap groups by occurred_at::date (plan §14.1), not
-- created_at — an index on created_at wouldn't serve that query, since
-- offline-flushed earn events can have occurred_at diverge from created_at
-- within the bound above. Indexing occurred_at instead of the plan's
-- literal "(user_id, kind, created_at)" is what actually backs the cap
-- check and the balance sum it's built alongside.
CREATE INDEX IF NOT EXISTS gem_events_user_kind_occurred_idx
  ON gem_events(user_id, kind, occurred_at);

ALTER TABLE gem_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own gem events"
  ON gem_events FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

-- No INSERT/UPDATE/DELETE policy — writable only by SECURITY DEFINER
-- functions (owned by postgres, bypass RLS and grants on tables postgres
-- owns) per plan §14.2.
GRANT SELECT ON public.gem_events TO authenticated, service_role;

-- ── user_inventory ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_inventory (
  user_id     uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  item_id     text REFERENCES shop_items(id) NOT NULL,
  qty         integer NOT NULL DEFAULT 0 CHECK (qty >= 0),
  acquired_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, item_id)
);

ALTER TABLE user_inventory ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own inventory"
  ON user_inventory FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

-- No INSERT/UPDATE/DELETE policy — writable only by SECURITY DEFINER
-- functions, same rationale as gem_events above.
GRANT SELECT ON public.user_inventory TO authenticated, service_role;

-- ── item_consumptions ─────────────────────────────────────────────────────
-- consume_item's idempotency/replay log. Deliberately separate from
-- gem_events: consuming an already-owned item isn't a gem movement, and
-- gem_events' own CHECK (delta <> 0) and its five-value kind enum have no
-- slot for a non-payment "item was used" event. Same server-side key
-- namespacing as gem_events (id = auth.uid() || ':' || p_idempotency_key)
-- for the same reason — a raw client-supplied key must never be usable to
-- read or collide with another user's row.

CREATE TABLE IF NOT EXISTS item_consumptions (
  id         text PRIMARY KEY,
  user_id    uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  item_id    text REFERENCES shop_items(id) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE item_consumptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own consumptions"
  ON item_consumptions FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

-- No INSERT/UPDATE/DELETE policy — writable only by consume_item.
GRANT SELECT ON public.item_consumptions TO authenticated, service_role;
