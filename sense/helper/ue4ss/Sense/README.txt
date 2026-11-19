Sense helper for Phantom Blade Zero (UE4SS Lua mod)
====================================================

What it does
- Game Guide's "Sense" needs to know where the player, the camera and the nearby items are.
  This mod reads that from the game 10 times a second and writes it to a small file:
      ue4ss\Mods\Sense\out\sense.json
  Game Guide reads that file and plays the sound / shows the pulse.
- It ONLY READS. It never changes anything in the game: no cheats, no hooks, no writes to game memory.
- Items counted: the five kinds of item this game really has (found with F3 and a live memory read):
    * treasure chests          (BP_Chest_C)                      -> "collectible"
    * Memory Flowers           (BP_FlowInteract_MemoryFlower_C)  -> "collectible"
    * hanging gifts            (BP_HangingGift_C)                -> "pickup"
    * loot spots               (BP_FlowInteractActor_C)          -> "pickup", but ONLY when the spot has a
      loot item (LootItemID). The same class is also used for talk spots and things you can look at;
      those have no loot item and are not counted.
    * white glowing loot piles   (BP_Rubbish_C, despite the name)   -> "pickup"
  Every 2 s the mod asks the game for exactly these five classes. That is quick. It does NOT look
  through all actors any more (that took about 0.08 s of game time each time).
- Opened chests and taken items are left out:
    * a chest counts as opened when CurSwitchState is true, or the chest is hidden. bInteracted is only
      shown as a length in the debug info: reading its entries uses the same code path that crashed
      this game, so the helper never does that;
    * a loot pile is left out when bDestroyed is true (the game sets it when you take the pile) or it is hidden;
    * a flower, gift or loot spot is left out when it is gone or hidden.
  This is checked at most once a second per item, only for items within 30 m.
- The rule "CurSwitchState true = opened" is still a guess until it is checked in the game. If 3 or
  more chests within 30 m all read CurSwitchState = true, UE4SS.log says so once ("warning: all N
  chests ..."). If you see that near chests you have NOT opened, press F3 near a closed chest and add
  section (g) of out\classes.txt to an issue on the Game Guide GitHub page.
- Never counted, even if a search finds them: talk/NPC spots (BP_FlowInteractActor_Character_C, the
  scarecrow, HuanShuPingZhang), breakable boxes (every ...ChaoSpawner... / ...ChaosSpawner... class),
  trigger boxes, the sky backdrop, water triggers and a few other helper objects. This is the
  "not_items" list (exact class names) and the "not_item_words" list (pieces of a name) in
  profiles\pbz.json.
- Items are listed up to 30 m from the player.

The game profile (profiles\<game>.json) and the learned rules (learned\)
- The item classes, their "opened/taken" rules, the classes that are never items, the doors/locks and
  the quest target are NOT in Scripts\main.lua any more. They are in the game's profile file:
      profiles\pbz.json        (Phantom Blade Zero; game.txt in this folder says which game it is)
      profiles\_default.json   (the word lists used to guess item and door classes in any game)
  If profiles\pbz.json is missing or broken, the mod uses a built-in copy of it (discovery stays off)
  and UE4SS.log says so once.
- learned\<game>.json holds rules the mod learned by itself while you played (for example "this class
  is taken when bOpened turns true"). It is written by the mod; delete the file to forget them.
  Profile seeds never learn: their rules only come from the profile. If the learned\ folder is
  missing, the mod writes out\learned_<game>.json instead.

How to add an item class
- Press F3 next to the item and look up its class name in out\classes.txt (the first word of its line).
- Open profiles\pbz.json and add one entry to the "seeds" list, for example:
      { "name": "BP_SomeNewThing_C", "kind": "pickup",
        "gone": [ { "type": "hidden" } ] }
  kind is "pickup" or "collectible". "require": [ { "type": "nameSet", "prop": "LootItemID" } ] means
  "only when it has a loot item". "gone" lists the rules that mean opened/taken (see BP_Chest_C there).
- If a class is wrongly counted, put its name in "not_items" in profiles\pbz.json.
- Restart the game after editing the profile (it is read once at start).

Switches at the top of Scripts\main.lua
- DEBUG_STATE (now true): every item in sense.json also gets a "state" part with the raw values the
  mod reads (CurSwitchState, bInteracted_n, bInteracted_true, startInteract, hidden, hasDestory, loot,
  gone). This is only there to check the "opened" rule. Set it to false when the rule is confirmed;
  the rest of sense.json stays exactly the same.
- SCAN_ACTORS (now false): true turns the old search through ALL actors back on (every 10 s). Only do
  that to hunt for item classes that the profile does not know yet; it costs game time. It keeps class
  names with pick, collect, treasure, loot, chest, drop, bonus or item in them (SCAN_IGNORE lists
  names it must skip). The newer way is "discovery": { "mode": "auto" } in profiles\pbz.json (also
  costs game time; it is "off" for PBZ).
- If the profile's seeds find nothing at all for 30 s (another game version, a level without items), the
  mod also tries the old guessed class names and interact parts. UE4SS.log then says so once.
- Every sense.json message also has "classes": the class names it found and how many of each,
  so you can see what the mod is counting without pressing F3.

Files
- Scripts\main.lua   the mod (with its helper parts Scripts\sense_*.lua)
- profiles\          the game profiles (pbz.json, _default.json), see above
- learned\           the rules learned while playing (learned\<game>.json)
- game.txt           which game this is (first word, e.g. pbz; missing = pbz)
- enabled.txt        tells UE4SS to load it (delete it, or set "Sense : 0" in Mods\mods.txt, to turn it off)
- out\sense.json     the output file (written as sense.json.tmp first, then renamed, so it is never half written)
- out\classes.txt    the discovery dump (see below)

Discovery dump (F3) - safe to press at any time
- Stand next to a visible pickup or a treasure chest and press F3 once. The mod writes out\classes.txt:
    (a) every actor class with a count, and every actor whose class name looks like a pickup/treasure
        (nearest to the player first, with its full name and position; the game's "default objects",
        which are not real actors in the world, are counted but left out of the lists);
    (b) the first interact parts (PBZInteractComponent / PBZInteractRegion): their owner actor, the full
        list of their properties and the value of every property named like type/kind/interact;
    (c) the same property lists for the first actors of the most common matching classes;
    (d) the screen size and the world-to-screen call as the game reports them;
    (e) what Sense is counting right now;
    (f) every actor class with its count;
    (g) every item within 30 m: SENT or HELD BACK (opened / hidden), with the state values.
        Press F3 once before and once after opening a chest to see what changed.
  Each part is written to the file as soon as it is done. If one part fails, the error text goes into
  the file and the next part still runs. One press = one dump (a short hitch is normal). A press while
  a dump is still running is ignored (the log says so); if a dump never reports back within 30 s, the
  next press starts a fresh one.
- The dump only asks the game for actors by class. It never walks through every object in memory
  (an older version did that and the game crashed), so F3 cannot crash the game that way any more.
- Press F3 again after picking an item up and compare the two dumps to see what changed.

Where to look when something is wrong
- ue4ss\UE4SS.log: every line from this mod starts with "[Sense]".
  "loaded (read-only)" = the mod started.  "error: ..." = something failed (at most one line per 5 s).
  "item classes now: ..." = the class names it found (one line each time the set changes).
  "first actor name scan done: N actors, M matched" = the 10 s scan ran for the first time (only with
  SCAN_ACTORS = true).
  "the profile's item classes found nothing for 30 s" = the old guessed names are being tried (see above).
  "warning: all N chests within 30 m read CurSwitchState = true" = see the chest rule under "What it does".
  "skipped one tick: file busy ..." = Game Guide was reading sense.json at that exact moment, so the old
  file was kept and the next tick (100 ms later) wrote a new one. Harmless, at most one line per 5 s.
- If out\sense.json is missing, make sure the folder ue4ss\Mods\Sense\out\ exists.
- When there is no player yet (main menu, loading) sense.json says "error": "no player yet". That is normal.
- The 10 s search through all actors is off (SCAN_ACTORS = false at the top of Scripts\main.lua).
- "profile: using the built-in pbz profile" = profiles\pbz.json is missing or broken; the built-in copy is used.
