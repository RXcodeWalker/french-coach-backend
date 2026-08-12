/*
  # Shop Phase 1 — seed the 19 launch items (plan §6)

  Prices, requirements and totals below were cross-checked against the
  plan's stated category totals (Gear 950 · Avatars 8,450 · Frames 3,300 ·
  Nameplates 3,250 = 15,950) and match exactly. Ids are this migration's own
  invention (the plan names items only in French display copy + emoji, e.g.
  "🦊 Le Renard") — display copy itself belongs to shopCatalogue.ts (Phase 2,
  plan §14.6's ownership model: server owns id/price/kind/requirement,
  client owns presentation), not seeded here. `emoji` is populated only for
  kind='avatar' rows, the one slot equip_cosmetic writes as a literal glyph
  rather than an id reference (see the schema migration's header).

  requirement->>'achievement' values are existing ids from
  src/data/achievements.ts, verified present: triple_jour, examinateur,
  causeur, grammaire_maitrisee, probleme_resolu, niveau_b2, bete_de_mode,
  semaine_parfaite, expert, grand_oral, fluent, perfectionniste,
  drill_master.

  max_owned = 1 for every cosmetic (avatar/frame/nameplate); NULL
  (unlimited) for the three consumables.
*/

INSERT INTO shop_items (id, kind, price_gems, consumable, max_owned, requirement, emoji, sort_order) VALUES
  -- Gear
  ('streak_freeze',        'consumable', 200,  true,  NULL, '{}'::jsonb,                                          '❄️', 10),
  ('focus_token',          'consumable', 150,  true,  NULL, '{}'::jsonb,                                          '🎯', 20),
  ('streak_repair',        'consumable', 600,  true,  NULL, '{}'::jsonb,                                          '🧵', 30),

  -- Identity — avatars
  ('avatar_croissant',     'avatar',     150,  false, 1,    '{}'::jsonb,                                          '🥐', 100),
  ('avatar_renard',        'avatar',     400,  false, 1,    '{"achievement":"triple_jour"}'::jsonb,               '🦊', 110),
  ('avatar_examinateur',   'avatar',     500,  false, 1,    '{"achievement":"examinateur"}'::jsonb,               '📖', 120),
  ('avatar_micro',         'avatar',     800,  false, 1,    '{"achievement":"causeur"}'::jsonb,                   '🎙️', 130),
  ('avatar_plume',         'avatar',     900,  false, 1,    '{"achievement":"grammaire_maitrisee"}'::jsonb,       '🖋️', 140),
  ('avatar_phenix',        'avatar',     1400, false, 1,    '{"achievement":"probleme_resolu"}'::jsonb,           '🔥', 150),
  ('avatar_hibou',         'avatar',     1800, false, 1,    '{"achievement":"niveau_b2"}'::jsonb,                 '🦉', 160),
  ('avatar_couronne',      'avatar',     2500, false, 1,    '{"achievement":"bete_de_mode"}'::jsonb,              '👑', 170),

  -- Identity — frames
  ('frame_ardoise',        'frame',      250,  false, 1,    '{}'::jsonb,                                          NULL, 200),
  ('frame_emeraude',       'frame',      450,  false, 1,    '{"achievement":"semaine_parfaite"}'::jsonb,          NULL, 210),
  ('frame_amethyste',      'frame',      1000, false, 1,    '{"achievement":"expert"}'::jsonb,                    NULL, 220),
  ('frame_or',              'frame',      1600, false, 1,    '{"achievement":"grand_oral"}'::jsonb,                NULL, 230),

  -- Identity — nameplates
  ('nameplate_encre',      'nameplate',  250,  false, 1,    '{}'::jsonb,                                          NULL, 300),
  ('nameplate_cobalt',     'nameplate',  600,  false, 1,    '{"achievement":"fluent"}'::jsonb,                    NULL, 310),
  ('nameplate_aurore',     'nameplate',  1200, false, 1,    '{"achievement":"perfectionniste"}'::jsonb,           NULL, 320),
  ('nameplate_tricolore',  'nameplate',  1200, false, 1,    '{"achievement":"drill_master"}'::jsonb,              NULL, 330)
ON CONFLICT (id) DO NOTHING;
