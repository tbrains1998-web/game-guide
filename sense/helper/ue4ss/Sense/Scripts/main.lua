-- Sense: READ-ONLY helper for Game Guide's "Sense" (nearby item sound + pulse) and its quest path.
-- One helper for every Unreal (UE4SS) game; a game plugs in with an optional profile (profiles\<game>.json).
-- Every 100 ms it writes where the player, the camera, the nearby pickups/treasures and the current target are to
-- <this mod folder>\out\sense.json (write sense.json.tmp, then rename, so the reader never sees a half file).
-- It never changes the game: it only reads positions and a few on/off values.
--
-- The work is split over small modules next to this file (all named sense_*.lua):
--   sense_json    JSON writer/reader          sense_files    mod folder, atomic file writes, JSON files
--   sense_ue      every game read (in pcall)  sense_profile  profiles\_default.json + profiles\<game>.json, learned\
--   sense_discover which classes are items/doors (tier A: FindAllOf per known class every 2 s; tier B: rare,
--                  budgeted look over all actors, only when the profile says discovery "auto")
--   sense_state   "taken/opened?" rules, learning new rules by watching, door "unlocked" flips
--   sense_target  the quest path target: what the game tracks (PBZ: tracked quest point / map lock), else the
--                 most recently unlocked door the player has not reached yet
--   sense_route   a walking route to the target over the game's walking map (navmesh), at most one round of
--                 navigation calls per second; off while Mods\Sense\route_off.txt exists
-- The game key comes from game.txt in this folder (first word; no file = "pbz").
-- Items: profile seeds (PBZ: chests, Memory Flowers, hanging gifts, loot spots, white loot piles), learned classes
-- and classes guessed this session. A chest that is already opened, a loot pile that is taken (bDestroyed), or an
-- item that is hidden (picked up), is not sent. Doors are never sent as items; they only feed the target.
-- The old ways (guessed PBZ class names + interact parts) only run in PBZ when the seeds find nothing for 30 s.
-- The old name scan over ALL actors is off: SCAN_ACTORS.
-- F3 = discovery dump to out\classes.txt (actors by class name, interact components with their property lists,
-- viewport/projection shapes, items, doors, target, learned rules). It only uses FindAllOf (class lookups). It
-- never walks every object in memory (that walk crashed this game on 2026-11-04), so F3 is safe at any time.
-- Lines in UE4SS.log start with "[Sense]".

local T0 = os.clock()
local function now() return string.format("%9.3f", os.clock() - T0) end
local function log(s) print("[Sense] " .. now() .. " " .. s .. "\n") end

-- settings
local TICK_MS     = 100     -- one sense.json per tick
local REBUILD_S   = 2.0     -- FindAllOf item scan this often (never a walk over every object at runtime)
local SCAN_S      = 10.0    -- FindAllOf("Actor") name scan this often (the biggest single call)
local SCAN_ACTORS = false   -- true = also the old name scan over ALL actors every 10 s (slow: ~0.08 s each time
                            -- in PBZ). The profile's discovery "auto" is the newer way to find unknown classes.
local STATE_S     = 1.0     -- "opened / picked up?" is read at most this often per item (only items within range)
local FALLBACK_S  = 30.0    -- PBZ: the seeds found nothing at all for this long -> also try the old guessed names
local DEBUG_STATE = true    -- true = every sent item also carries "state" (the raw opened/hidden values), and F3
                            -- writes section (g); used to confirm the "opened" rule. false = plain contract items.
local MAX_RANGE   = 30.0    -- metres; items farther away are not written (3 x the window's max slider)
local FAR_RANGE   = 60.0    -- items farther than this get their position re-read once a second only
local MAX_ITEMS   = 64      -- most items in one message (nearest first)
local ERROR_EVERY = 5.0     -- seconds between error messages/logs
local MAX_DUMP    = 6000    -- most lines in classes.txt
local MAX_SUPER   = 8       -- most class levels walked for one property list
local DISCOVER_TIMEOUT_S = 30.0   -- an F3 dump that has not finished after this long is given up, so F3 works again
local LEARN_RANGE = 8.0     -- learning: items/doors of guessed or learned classes within this many metres are watched
local LEARN_S     = 0.5     -- ... with one snapshot of their on/off values this often
local DOOR_S      = 1.0     -- a door's "unlocked" value is read this often
local TARGET_S    = 0.5     -- the game's own tracked target is read this often
local MAX_DOOR_EVENTS = 32  -- most remembered "door unlocked" events (newest kept)
local VERSION     = "ue4ss-sense/0.2"
local DEFAULT_GAME = "pbz"  -- game key when game.txt is missing
-- EInteractableObjectType as read from the exe: Undefined, Weapon, Bell, Switch, Gate, Pickup, Treasure, Interaction.
-- The discovery dump (F3) shows the real numbers; change these two if they differ.
local TYPE_PICKUP, TYPE_TREASURE = 5, 6
-- names tried (in pcall) to read a PBZInteractComponent's type; the first one that answers is kept
local TYPE_PROPS = { "InteractType", "InteractableType", "InteractableObjectType", "InteractObjectType", "ObjectType", "Type" }
local TYPE_FUNCS = { "GetInteractType", "GetInteractableType", "GetInteractableObjectType", "GetInteractObjectType", "GetObjectType" }
-- PBZ FALLBACK ONLY (the seeds found nothing for FALLBACK_S): class names tried with FindAllOf every 2 s
-- (a name this game does not have just gives nil). None of these exist in PBZ as of 2026-11-04.
-- No generic "interact" class here: the game has ONE interact system for weapons, bells, switches, gates, pickups
-- and treasures, so such a class would make every bell and gate an item. Interactables are only counted through
-- their PBZInteractComponent when its type says Pickup/Treasure (step 3 of the item scan).
local FALLBACK_GAMES = { pbz = true }
local CANDIDATE_CLASSES = { "PBZPickupActor", "PBZPickUpActor", "PBZItemActor", "PBZDropItem", "PBZTreasureActor",
                            "PBZTreasureBox", "PBZChest", "PBZCollectible", "PBZBonusCollection",
                            "PBZLootActor", "PBZItem" }
-- the old runtime actor name scan keeps a class when its name contains one of SCAN_WORDS (any case) and none of
-- SCAN_EXCLUDE (exact case: UE class names are CamelCase, and "UI" must not hit "Equipment")
local SCAN_WORDS   = { "pick", "collect", "treasure", "loot", "chest", "drop", "bonus", "item" }
local SCAN_EXCLUDE = { "Manager", "Subsystem", "Widget", "UI", "Spawner", "Default__", "Volume", "Trigger" }
-- exact class names the scan must never keep (words like "drop" or "item" also hit rain drops, shops, pickaxes...).
local SCAN_IGNORE  = { }   -- e.g. { "BP_RainDrop_C", "BP_ItemShop_C" }
local COLLECT_WORDS = { "treasure", "chest", "collect", "bonus" }   -- these class names count as "collectible"
-- the discovery dump (F3) lists every actor whose class name contains one of these
local DUMP_WORDS = { "pick", "collect", "treasure", "interact", "loot", "flower", "item", "gather", "chest", "drop",
                     "bonus", "reward", "gift", "box", "door", "gate", "lock" }
local PROP_WORDS = { "type", "kind", "interact" }   -- property names whose value the dump reads
-- the property walk stops at these engine classes (their properties are known and not interesting)
local STOP_CLASSES = { Object = true, Actor = true, ActorComponent = true, SceneComponent = true, PrimitiveComponent = true }

-- ---------- where this mod lives (for the out\ folder and the modules) ----------
local function modDir()
    local dbg = rawget(_G, "debug")
    local ok, info = pcall(function() return dbg.getinfo(1, "S") end)   -- "@<game folder>\...\Mods\Sense\Scripts\main.lua"
    local src = ok and info and info.source or ""
    if src:sub(1, 1) == "@" then src = src:sub(2) end
    local dir = src:match("^(.+)[/\\]Scripts[/\\][^/\\]+$")
    if dir then return dir, "script path" end
    local pp = type(package) == "table" and package.path or ""         -- UE4SS puts <Mods>\shared on package.path
    local mods = pp:match("([^;]-[/\\]Mods)[/\\]shared[/\\]")
    if mods then return mods .. "\\Sense", "package.path" end
    return "ue4ss\\Mods\\Sense", "relative guess"                     -- the game's working folder is normally next to PBZ.exe
end

local OUT_DIR, OUT_HOW = modDir()
local OUT_FILE  = OUT_DIR .. "\\out\\sense.json"
local DUMP_FILE = OUT_DIR .. "\\out\\classes.txt"

-- the sense_*.lua modules sit next to this file; that UE4SS puts this folder on package.path is not proven, so
-- it is added here (first, so a module of another mod with the same name can never win)
pcall(function()
    local sp = OUT_DIR .. "\\Scripts\\?.lua"
    if type(package) == "table" and type(package.path) == "string" and not package.path:find(sp, 1, true) then
        package.path = sp .. ";" .. package.path
    end
end)

local loadProblems = {}
local function need(name)
    local ok, m = pcall(require, name)
    if ok and type(m) == "table" then return m end
    loadProblems[#loadProblems + 1] = name .. " (" .. tostring(m) .. ")"
    return nil
end
local json     = need("sense_json")
local files    = need("sense_files")
local UE       = need("sense_ue")
local profile  = need("sense_profile")
local discover = need("sense_discover")
local state    = need("sense_state")
local target   = need("sense_target")      -- optional: without it there is no target, items still work
local route    = need("sense_route")       -- optional: without it the quest path is the straight line
local READY = json ~= nil and files ~= nil and UE ~= nil and profile ~= nil and discover ~= nil and state ~= nil

local NULL  = json and json.NULL or setmetatable({}, { __name = "null" })
local array = json and json.array or function(t) return t or {} end
local toJson = json and json.encode or function() return "null" end
local ueFullName = UE and UE.fullName or function() return nil end

-- ---------- game key, profile, learned rules (files only, no game touches) ----------
local GAME, GAME_HOW = DEFAULT_GAME, "default"
local P, PINFO, L = nil, nil, nil
if READY then
    pcall(function() profile.setLog(log); discover.setLog(log) end)
    local okk, key = pcall(profile.readGameKey, OUT_DIR)
    if okk and type(key) == "string" and key ~= "" then GAME, GAME_HOW = key, "game.txt" end
    local okp, p, info = pcall(profile.load, OUT_DIR, GAME, log)
    if okp and type(p) == "table" then P, PINFO = p, info end
    if not P and type(profile.fallback) == "function" then
        -- profile.load itself failed: the built-in profile of this game (PBZ: discovery off) or the defaults
        log("profile load failed (" .. tostring(p) .. ")")
        local okf, pf, infof = pcall(profile.fallback, GAME, nil, nil, log)
        if okf and type(pf) == "table" then P, PINFO = pf, infof end
    end
    if not P then P = profile.normalize(profile.merge(profile.DEFAULTS, {}), log); P.game = GAME end
    local okl, l = pcall(profile.loadLearned, OUT_DIR, GAME, log)
    L = (okl and type(l) == "table") and l or profile.emptyLearned()
end
P = P or { seeds = {}, doors = {}, notItemSet = {}, not_item_words = {}, discovery = { mode = "off" } }
L = L or { v = 1, classes = {}, doors = {} }

local function notItem(cls)            -- not_items or a not_item_words piece (PBZ: breakable spawners)
    if not profile then return false end
    local ok, v = pcall(profile.notItem, P, cls)
    return ok and v == true
end

-- old-style view of the profile's item seeds (F3 dump and offline tests): chest = has the chest "opened" rules,
-- rubbish = "taken" is bDestroyed, needLoot = has a "require" rule (LootItemID set)
local legacyCache = setmetatable({}, { __mode = "k" })
local function legacyOf(entry)
    if type(entry) ~= "table" then return {} end
    local c = legacyCache[entry]
    if c then return c end
    c = {}
    for _, r in ipairs(type(entry.gone) == "table" and entry.gone or {}) do
        if type(r) == "table" then
            if r.type == "arrayAnyTrue" or (r.type == "bool" and r.prop == "CurSwitchState") then c.chest = true end
            if r.type == "bool" and r.prop == "bDestroyed" then c.rubbish = true end
        end
    end
    for _, r in ipairs(type(entry.require) == "table" and entry.require or {}) do
        c.needLoot = true
        if type(r) == "table" and r.type == "nameSet" and not c.lootProp then c.lootProp = r.prop end
    end
    legacyCache[entry] = c
    return c
end

local ITEM_CLASSES, ITEM_BY_NAME = {}, {}
for _, s in ipairs(P.seeds or {}) do
    local fl = legacyOf(s)
    local e = { name = s.name, kind = s.kind, needLoot = fl.needLoot == true, chest = fl.chest == true,
                rubbish = fl.rubbish == true }
    ITEM_CLASSES[#ITEM_CLASSES + 1] = e
    ITEM_BY_NAME[s.name] = e
end
local NOT_ITEMS = P.notItemSet or {}

-- ---------- JSON message ----------
local function vec(a) return array({ a[1], a[2], a[3] }) end
local learnedN = 0          -- learned classes + doors (sent as "learned" once something was learned)

-- One message (base fields + "classes" + the v2 additions). player = {x,y,z} metres or nil; cam = {pos={},
-- rot={pitch,yaw,roll}, fov=} or nil; screen = {w,h}; items = list of {id=, kind=, name=, pos={x,y,z}, dist=}
-- nearest first; err = text or nil; classes = {class name -> how many items of it are in the cache} (always
-- written, {} when nothing was found); target = a sense_target table, NULL (= "target": null) or nil (no key);
-- footDz = metres from the player point down to the feet, or nil (no key).
-- routeInfo = a sense_route route {pts={{x,y,z},...} metres, src="nav"|"nav_partial"|"ground"} or nil: only used when
-- target is a table; then the target gets "route": [[x,y,z],...] and "route_src" (a copy, the target is not changed).
-- elsewhere = {label=, map=} or nil (LAST param): the tracked main quest is on another map
-- (sense_target.elsewhere()); sent as the top-level "target_elsewhere": {"label","map"} key, absent when nil.
-- Without a player (menus, loading) the message has "error" and no "player"/"cam"; "items" is always there.
-- An item may carry "state" (the raw opened/hidden values) only when DEBUG_STATE is on and the caller gave one.
local ROUTE_SRC = { nav = true, nav_partial = true, ground = true }
local ROUTE_MAX = 64
local function finiteNum(v) return type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge end
-- a route -> JSON array of [x,y,z] arrays, or nil when it is not usable (fewer than 2 good points, bad src)
local function routeJson(r)
    if type(r) ~= "table" or type(r.pts) ~= "table" or not ROUTE_SRC[r.src] then return nil end
    local out = array()
    local n = #r.pts
    if n > ROUTE_MAX then n = ROUTE_MAX end
    for i = 1, n do
        local p = r.pts[i]
        if type(p) ~= "table" then return nil end
        local x, y, z = p[1], p[2], p[3]
        if not (finiteNum(x) and finiteNum(y) and finiteNum(z)) then return nil end
        out[i] = array({ x, y, z })
    end
    if #out < 2 then return nil end
    return out
end

local function buildMessage(seq, tMs, player, cam, screen, items, err, classes, target, footDz, routeInfo, elsewhere)
    local list = array()
    for i, it in ipairs(items or {}) do
        list[i] = { id = it.id, kind = it.kind, name = it.name or "", pos = vec(it.pos), dist = it.dist, screen = NULL }
        if DEBUG_STATE and type(it.state) == "table" then list[i].state = it.state end
    end
    local cls = {}
    for k, n in pairs(classes or {}) do cls[tostring(k)] = n end
    local msg = { v = 1, game = GAME, helper = VERSION, seq = seq, t = tMs, coords = "ue",
                  screen = array({ screen[1] or 0, screen[2] or 0 }), range = MAX_RANGE, items = list, classes = cls }
    if player then msg.player = vec(player) end
    if cam then msg.cam = { pos = vec(cam.pos), rot = array({ cam.rot[1], cam.rot[2], cam.rot[3] }), quat = NULL, fov = cam.fov } end
    if err then msg.error = err end
    if target ~= nil then msg.target = target end
    if type(target) == "table" and target ~= NULL and routeInfo ~= nil then
        local rj = routeJson(routeInfo)
        if rj then
            local t2 = {}
            for k, v in pairs(target) do t2[k] = v end
            t2.route, t2.route_src = rj, routeInfo.src
            msg.target = t2
        end
    end
    if type(footDz) == "number" and footDz == footDz then msg.player_foot_dz = footDz end
    if type(elsewhere) == "table" then
        local lb, mp = elsewhere.label, elsewhere.map
        msg.target_elsewhere = { label = type(lb) == "string" and lb or "", map = type(mp) == "string" and mp or "" }
    end
    if learnedN > 0 then msg.learned = { count = learnedN } end
    return toJson(msg)
end

-- write the whole text to path.tmp, then swap it in (sense_files): true, or false + text + busy. busy = the old
-- file is still there and whole (the reader had it open), the next tick simply tries again.
local function writeAtomic(path, text)
    if not files then return false, "sense_files.lua is missing", false end
    return files.writeAtomic(path, text)
end

-- ---------- safe game access (every UObject touch is inside pcall) ----------
local UEHelpers = nil
pcall(function() UEHelpers = require("UEHelpers") end)

local function valid(obj)
    local ok, v = pcall(function() return obj ~= nil and obj:IsValid() end)
    return ok and v == true
end

-- call a no-argument UFunction; nil when it is missing or fails
local function call(obj, fname)
    local ok, r = pcall(function() return obj[fname](obj) end)
    if ok then return r end
    return nil
end

local function readVec(v)   -- UE FVector (cm) -> {x,y,z} metres, or nil
    local ok, x, y, z = pcall(function() return v.X, v.Y, v.Z end)
    if ok and type(x) == "number" and type(y) == "number" and type(z) == "number" then
        return { x / 100, y / 100, z / 100 }
    end
    return nil
end

local function readRot(r)   -- UE FRotator -> {pitch, yaw, roll} degrees, or nil
    local ok, p, y, ro = pcall(function() return r.Pitch, r.Yaw, r.Roll end)
    if ok and type(p) == "number" and type(y) == "number" and type(ro) == "number" then return { p, y, ro } end
    return nil
end

local function outNum(v)    -- an out-param table (or a plain number) -> its number, or nil
    if type(v) == "number" then return v end
    if type(v) == "table" then
        for _, x in pairs(v) do if type(x) == "number" then return x end end
    end
    return nil
end

local function className(obj)   -- short class name of a UObject ("BP_Chest_C"), or nil
    local ok, s = pcall(function() return obj:GetClass():GetFName():ToString() end)
    if ok and type(s) == "string" and s ~= "" then return s end
    return nil
end

local function fullName(obj)    -- GetFullName(), or "?"
    local ok, s = pcall(function() return obj:GetFullName() end)
    if ok and type(s) == "string" then return s end
    return "?"
end

local function locCm(obj)       -- actor location in cm {x,y,z}, or nil (not an actor, or the call failed)
    local ok, x, y, z = pcall(function() local l = obj:K2_GetActorLocation(); return l.X, l.Y, l.Z end)
    if ok and type(x) == "number" and type(y) == "number" and type(z) == "number" then return { x, y, z } end
    return nil
end

local function fmtLoc(l) return string.format("%.0f,%.0f,%.0f", l[1], l[2], l[3]) end

local function locStr(obj)
    local l = locCm(obj)
    if l then return fmtLoc(l) end
    return "?"
end

local function anyWord(s, words)        -- any case: does s contain one of the words?
    local low = s:lower()
    for _, w in ipairs(words) do if low:find(w, 1, true) then return true end end
    return false
end

local function anyWordExact(s, words)   -- exact case
    for _, w in ipairs(words) do if s:find(w, 1, true) then return true end end
    return false
end

local function kindFromName(cls)        -- class name -> "collectible" (treasure/chest/collect/bonus) or "pickup"
    if anyWord(cls, COLLECT_WORDS) then return "collectible" end
    return "pickup"
end

local function scanClassOk(cls)         -- does the old runtime actor name scan keep this class?
    if notItem(cls) then return false end
    if ITEM_BY_NAME[cls] then return true end
    for _, n in ipairs(SCAN_IGNORE) do if cls == n then return false end end
    return anyWord(cls, SCAN_WORDS) and not anyWordExact(cls, SCAN_EXCLUDE)
end

local function countKeys(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

local function byCount(counts)          -- sort order: most common first, then by name
    return function(a, b)
        if counts[a] ~= counts[b] then return counts[a] > counts[b] end
        return a < b
    end
end

local function findPC()
    local pc = nil
    if UEHelpers then pcall(function() pc = UEHelpers.GetPlayerController() end) end
    if not valid(pc) then pcall(function() pc = FindFirstOf("PlayerController") end) end
    if valid(pc) then return pc end
    return nil
end

-- viewport size in pixels; {0,0} when unknown (the app then uses its own monitor size)
local function readScreen(pc)
    local ox, oy = {}, {}
    local r = { pcall(function() return pc:GetViewportSize(ox, oy) end) }
    local w, h = outNum(ox), outNum(oy)
    if r[1] and not (w and h) then          -- some builds hand out-params back as extra return values
        local nums = {}
        for i = 2, #r do if type(r[i]) == "number" then nums[#nums + 1] = r[i] end end
        if #nums >= 2 then w, h = nums[#nums - 1], nums[#nums] end
    end
    if not (w and h and w > 0 and h > 0) then   -- fall back to the resolution in the game's settings
        local okr, res = pcall(function() return UEHelpers.GetEngine().GameUserSettings:GetScreenResolution() end)
        if okr then
            local okxy, x, y = pcall(function() return res.X, res.Y end)
            if okxy and type(x) == "number" and type(y) == "number" then w, h = x, y end
        end
    end
    if w and h and w > 0 and h > 0 then return { math.floor(w), math.floor(h) } end
    return { 0, 0 }
end

local function kindFromType(v)   -- interact type value -> "pickup" / "collectible" / nil (= not counted)
    if type(v) == "number" then
        if v == TYPE_TREASURE then return "collectible" elseif v == TYPE_PICKUP then return "pickup" end
    elseif type(v) == "string" then
        local l = v:lower()
        if l:find("treasure", 1, true) then return "collectible" elseif l:find("pickup", 1, true) then return "pickup" end
    end
    return nil
end

local typeAccessor = nil   -- {how = "prop"|"func", name = ...} once something answered
local typeWarned = false

local function readType(comp)
    if typeAccessor then
        local ok, v = pcall(function()
            if typeAccessor.how == "prop" then return comp[typeAccessor.name] end
            return comp[typeAccessor.name](comp)
        end)
        if ok then return v end
        return nil
    end
    for _, n in ipairs(TYPE_PROPS) do
        local ok, v = pcall(function() return comp[n] end)
        if ok and (type(v) == "number" or type(v) == "string") then
            typeAccessor = { how = "prop", name = n }
            log("interact type read from property " .. n .. " (first value " .. tostring(v) .. ")")
            return v
        end
    end
    for _, n in ipairs(TYPE_FUNCS) do
        local ok, v = pcall(function() return comp[n](comp) end)
        if ok and (type(v) == "number" or type(v) == "string") then
            typeAccessor = { how = "func", name = n }
            log("interact type read from function " .. n .. " (first value " .. tostring(v) .. ")")
            return v
        end
    end
    return nil
end

local function orNull(v) if v == nil then return NULL end return v end
local function finite(n) return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge end

-- ---------- the caches ----------
local items = {}            -- id -> {obj=, id=, kind=, cls=, entry=, name=, pos=, dist=, nextCheck=, gone=, state=,
                            --        nextState=, slowRules=, callRules=, noCall=, learn=, snap=, nextSnap=, loot=}
local doors = {}            -- id -> {obj=, id=, cls=, kind="door", entry=, nextRead=, snap=, nextSnap=, dist=}
local doorEvents = {}       -- "unlocked" flips seen this session (sense_state.doorStep events), newest last
local doorTracker = state and state.newDoorTracker() or { last = {}, n = 0 }
local session = { ctx = { ue = UE } }   -- sense_discover's memory for this game session
local SCTX = { ue = UE }                -- sense_state context for item rules
-- sense_state context for learning snapshots: with L, so each object's first-seen values are kept per session by
-- its id and survive the item/door table being rebuilt
local LCTX = { ue = UE, L = L }
local VANISH_NEAR_S = 3.0   -- a "vanish" door fires only when the player was within LEARN_RANGE of it this
                            -- recently (a door that streams out with its level part far away is not "unlocked")
local tracked, lastTargetAt = nil, -math.huge   -- what the game itself tracks (read every TARGET_S)
local lastTarget = nil      -- the target of the last message (F3)
local trackedElsewhere = nil -- {label, map}: the tracked main quest is on another map (sense_target.elsewhere(),
                             -- read together with `tracked`); sent as "target_elsewhere", and no target meanwhile
local lastRoute = nil       -- the walking route of the last message (F3)
local mapName = nil         -- the pawn's map (package part of its full name); a change clears door events
local footDz = nil          -- metres from the player point down to the feet (capsule half height)
local learnedDirty = false  -- L changed and could not be saved yet
local firstRebuildAt = nil  -- os.clock() of the first item scan with a player
local lastItemFind = nil    -- os.clock() of the last item scan where the seeds found anything
local fallbackLogged = false
local allOpenLogged = false -- the "every chest in range reads CurSwitchState true" warning is logged once
local ALL_OPEN_MIN = 3      -- that warning needs at least this many chests within range
local scanItems = {}       -- the part of items that came from the actor name scan (kept between scans)
local classCounts = {}      -- class name -> how many items of it are in the cache (the "classes" key)
local pc = nil
local screen = { 0, 0 }
local lastRebuild = -math.huge
local lastScan = -math.huge
local scanLogged = false
local lastClassSig = nil
local seq = 0
local TCTX = {              -- sense_target context
    log = function(s) log(s) end,
    array = function(t) return array(t) end,
    -- nil = nothing loaded (not a failure); a real FindAllOf error is raised so sense_target can count it
    findAll = function(name)
        local ok, l = pcall(FindAllOf, name)
        if not ok then error("FindAllOf(" .. tostring(name) .. ") failed: " .. tostring(l), 0) end
        return l
    end,
}
local loggedOnce = {}
local function logOnce(key, s) if not loggedOnce[key] then loggedOnce[key] = true; log(s) end end

if READY then learnedN = state.learnedCount(L) end

local function msNow() return math.floor((os.clock() - T0) * 1000) end

local function isSeed(entry) return type(entry) == "table" and entry.seed == true end

-- work out an item's rules once per item scan: callRules are checked every tick (like the old IsValidPickup
-- check: a function that fails once on this item is not tried again on it), the other rules once per STATE_S
local function prepare(it)
    local rules = {}
    local okr, r = pcall(state.goneRules, it, it.entry, { L = L })
    if okr and type(r) == "table" then rules = r end
    local slow, calls = {}, {}
    for _, x in ipairs(rules) do
        if x.type == "call" then calls[#calls + 1] = x
        elseif x.type ~= "vanish" then slow[#slow + 1] = x end
    end
    it.slowRules, it.callRules = slow, calls
    it.learn = not isSeed(it.entry)
    local fl = legacyOf(it.entry)
    it.chest, it.rubbish = fl.chest == true, fl.rubbish == true
    if fl.lootProp and it.loot == nil then it.loot = UE.readName(it.obj, fl.lootProp) end
end

local function newItem(id, cls, kind)
    return { id = id, kind = kind, cls = cls, name = "", pos = nil, dist = math.huge, nextCheck = 0, nextState = 0 }
end

-- the per-tick check: true + why when the item is gone for sure (no longer valid, or a "call" rule such as
-- IsValidPickup answers its "gone" value)
local function fastDrop(it)
    if not valid(it.obj) then return true, "vanish" end
    for _, r in ipairs(it.callRules or {}) do
        if not (it.noCall and it.noCall[r.fn]) then
            local okv, v = pcall(function() local o = it.obj; return o[r.fn](o) end)
            if okv then
                if v ~= nil and type(v) == type(r.is) and v == r.is then return true, "call" end
            else
                it.noCall = it.noCall or {}
                it.noCall[r.fn] = true   -- this item has no such function: do not try it 10x a second
            end
        end
    end
    return false
end

-- ---------- learning (guessed / learned classes only; profile seeds never learn) ----------
local function onLearn(ev)
    if type(ev) ~= "table" or not ev.learned then return end
    local okk, key = pcall(state.ruleKey, ev.rule)
    log(string.format("learned: %s %s %s when %s (seen on 2 objects near the player)", ev.kind == "door" and "door class" or "class",
        tostring(ev.cls), ev.kind == "door" and "is unlocked" or "is taken", okk and tostring(key) or "?"))
    learnedN = state.learnedCount(L)
    learnedDirty = true
end

local function saveLearned()
    if not learnedDirty then return end
    local ok, a, b, busy = pcall(profile.saveLearned, OUT_DIR, GAME, L, log)
    if ok and a == true then
        learnedDirty = false
        log("learned rules saved to " .. tostring(b))
    elseif ok and busy then
        -- held open by someone: tried again on the next item scan
    else
        learnedDirty = false     -- the folder is not writable: keep the rules for this session only
        log("learned rules could not be saved (" .. tostring(ok and b or a) .. "); they stay for this session")
    end
end

local function learnSnap(it, t)
    it.nextSnap = t + LEARN_S
    local snap = state.snapshot(it, nil, LCTX)
    if type(snap) ~= "table" then return end
    if it.snap then onLearn(state.learnStep(it, it.snap, snap, it.dist, t, L)) end
    it.snap = snap
end

local function learnVanish(it, t)
    if not it.learn or not it.snap then return end
    onLearn(state.learnStep(it, it.snap, { valid = false, props = {} }, it.dist, t, L))
    it.snap = nil
end

-- ---------- doors: shared parts of stepDoors and the item scan ----------
-- the door's "unlocked" rule: its profile entry, else what was learned for its class, else nil
local function doorRule(d)
    if type(d.entry) == "table" and type(d.entry.unlocked) == "table" then return d.entry.unlocked end
    local ld = type(L.doors) == "table" and L.doors[d.cls] or nil
    if type(ld) == "table" and type(ld.unlocked) == "table" then return ld.unlocked end
    return nil
end

local function addDoorEvent(ev)
    if type(ev) ~= "table" then return end
    doorEvents[#doorEvents + 1] = ev
    while #doorEvents > MAX_DOOR_EVENTS do table.remove(doorEvents, 1) end
    local p = type(ev.pos) == "table" and ev.pos or {}
    log(string.format("door unlocked: %s %s at %.1f,%.1f,%.1f m", tostring(ev.kind), tostring(ev.id),
        tonumber(p[1]) or 0, tonumber(p[2]) or 0, tonumber(p[3]) or 0))
end

-- a door whose object is no longer valid. NOTHING is read from it (a destroyed / collected actor can crash
-- the game in a way pcall does not catch); only IsValid. A "vanish" unlocked rule still fires, with the position
-- kept from the last good read (d.pos), and a guessed door watched near the player gets its "vanish" learning step.
-- Runs once per door object (d.dead).
local function doorGone(d, t)
    if d.dead then return end
    d.dead = true
    local rule = doorRule(d)
    if rule then
        -- only when the player was near it (within LEARN_RANGE in the last VANISH_NEAR_S seconds, d.nearAt
        -- from stepDoors); a door that vanished far away most likely streamed out with its level part
        local nearAt = d.nearAt
        local wasNear = type(nearAt) == "number" and type(t) == "number" and t - nearAt >= 0
                        and t - nearAt <= VANISH_NEAR_S
        if rule.type == "vanish" and type(d.pos) == "table" and not wasNear then
            logOnce("vanishFar", "a door with a 'vanish' unlocked rule vanished while the player was not near it: " ..
                "not counted as unlocked (level part streamed out?) (logged once)")
        end
        if rule.type == "vanish" and type(d.pos) == "table" and wasNear then
            local e = type(d.entry) == "table" and d.entry or nil
            -- a stand-in without pos_prop / id_prop, so sense_state reads nothing but IsValid from the object
            local proxy = { obj = d.obj, cls = d.cls, id = d.id, pos = d.pos,
                            entry = { unlocked = rule, kind = e and e.kind or nil } }
            addDoorEvent(state.doorStep(doorTracker, proxy, t, { ue = UE, L = L }))
        end
    elseif not isSeed(d.entry) and d.snap then
        onLearn(state.learnStep(d, d.snap, { valid = false, props = {} }, d.dist, t, L))
        d.snap = nil
    end
end

-- ---------- opened/hidden state of an item (rules from the profile / learned file, read via sense_state) ----------
-- read the item's state now: sets it.gone (true = do not send: opened chest / hidden item) and it.state (raw values)
local function checkState(it, t)
    local gone, detail = false, {}
    local okg, g, d = pcall(state.isGone, it, { gone = it.slowRules or {}, implicit = false }, SCTX)
    if okg then gone, detail = g == true, type(d) == "table" and d or {} end
    it.gone = gone
    local cur = detail["bool:CurSwitchState:true"]
    if type(cur) ~= "boolean" then cur = nil end
    it.curOn = it.chest and cur == true or nil   -- for the "every chest in range reads CurSwitchState true" warning
    if DEBUG_STATE then
        -- the raw values: the old PBZ columns first (Game Guide's debug view and the F3 dump read them), then
        -- every other bool/number/collision rule of this item by its property name
        local n = detail["arrayAnyTrue:bInteracted:"]
        local trues = nil
        if type(n) == "number" and n <= 0 then trues = 0 end
        local start, destroyed = nil, nil
        if it.chest then start = UE.readBool(it.obj, "Start Interact") end
        if it.rubbish then destroyed = detail["bool:bDestroyed:true"]   -- loot piles: bDestroyed in the same column
        elseif not it.chest then destroyed = UE.readBool(it.obj, "HasDestory") end   -- Memory Flowers (game's spelling)
        local hidden = detail["hidden::true"]
        local st = { CurSwitchState = orNull(cur), bInteracted_n = orNull(n), bInteracted_true = orNull(trues),
                     startInteract = orNull(start), hidden = orNull(hidden), hasDestory = orNull(destroyed),
                     loot = orNull(it.loot), gone = gone }
        for _, r in ipairs(it.slowRules or {}) do
            local okk, key = pcall(state.ruleKey, r)
            local raw = nil
            if okk then raw = detail[key] end   -- keeps a false value (an "and/or" would turn it into nil)
            if (r.type == "bool" or r.type == "num") and r.prop ~= "CurSwitchState" and r.prop ~= "bDestroyed"
                and st[r.prop] == nil and (type(raw) == "boolean" or finite(raw)) then
                st[r.prop] = raw
            elseif r.type == "collision" and type(raw) == "boolean" then
                st.collision = raw
            end
        end
        it.state = st
    else
        it.state = nil
    end
    -- next look in STATE_S to 1.25 x STATE_S (spread a little so not every item is read in the same tick; the spread
    -- only adds time, so an item is never read more than once per STATE_S)
    it.nextState = t + STATE_S * (1 + 0.25 * math.random())
end

-- ---------- the item scan ----------
-- every 2 s: tier A of sense_discover (FindAllOf for every profile seed, profile door, learned class and class
-- guessed this session). Only in PBZ, and only when that found nothing at all for FALLBACK_S: also the old guessed
-- class names and interact components of type Pickup/Treasure (their owner actor). With SCAN_ACTORS on, every
-- 10 s the old name scan over all actors as well. Items found by that scan stay cached between scans.
local function rebuild()
    local t = os.clock()
    local fresh, freshDoors = {}, {}
    local function guessEntry(kind) return { kind = kind, guess = true, require = {}, gone = P.guess_gone or {} } end
    -- add an actor found by the fallback / name scan; true when it is in there. id = its GetFullName() when the
    -- caller already has it. The actor's OWN class wins: a not_items class is never added, a door is never an
    -- item, and a known class (seed / learned / guessed) uses its own entry. For a guessed entry: kind from the
    -- interact component's own type (sure = true) always wins, and a name-based "collectible" beats a name-based
    -- "pickup", so a treasure found through a generic parent class is a collectible.
    local function add(obj, entry, fromScan, id, sure)
        if id == nil then
            local okn, s = pcall(function() return obj:GetFullName() end)
            if okn then id = s end
        end
        -- class default objects are not in the world; TrashedPackage = an unloaded level part: never touch those
        if type(id) ~= "string" or id:find("Default__", 1, true) or id:find("TrashedPackage", 1, true) then return false end
        local cls = id:match("^(%S+)") or "?"
        if notItem(cls) then return false end
        local okE, own = pcall(discover.entryFor, cls, P, L, session)
        if okE and type(own) == "table" then
            if own.door then return false end
            entry = own
        end
        local kind = entry.kind
        local it = fresh[id]
        if not it then
            if not state.requireOk(obj, entry, SCTX, id) then return false end
            if entry.guess and not sure and kind ~= "collectible" and kindFromName(cls) == "collectible" then kind = "collectible" end
            it = items[id] or newItem(id, cls, kind)
            it.obj, it.entry = obj, entry
            fresh[id] = it
            if fromScan then scanItems[id] = it end
        end
        if entry.guess then
            if sure then it.kind, it.sure = kind, true
            elseif not it.sure and kind == "collectible" then it.kind = kind end
        end
        prepare(it)
        return true
    end
    -- 1) tier A: seeds, doors, learned and session classes (each actor judged by its own class name)
    local found = 0
    local okA, list = pcall(discover.tierA, P, L, session)
    if okA and type(list) == "table" then
        for id, f in pairs(list) do
            if type(id) == "string" and type(f) == "table" then
                if f.kind == "door" then
                    local d = doors[id] or { id = id, kind = "door", nextRead = 0, nextSnap = 0 }
                    if d.obj ~= f.obj then d.dead = nil end   -- a new object under the same name is read again
                    d.obj, d.cls, d.entry = f.obj, f.cls, f.entry
                    freshDoors[id] = d
                elseif not fresh[id] then
                    local it = items[id] or newItem(id, f.cls, f.kind)
                    it.obj, it.entry, it.kind = f.obj, f.entry, f.kind
                    prepare(it)
                    fresh[id] = it
                    found = found + 1
                end
            end
        end
    elseif not okA then
        logOnce("tierA", "item scan failed once (logged once): " .. tostring(list))
    end
    firstRebuildAt = firstRebuildAt or t
    if found > 0 then lastItemFind = t end
    local fallback = t - (lastItemFind or firstRebuildAt) >= FALLBACK_S
    fallback = fallback and FALLBACK_GAMES[GAME] == true
    if fallback and not fallbackLogged then
        fallbackLogged = true
        log(string.format("the profile's item classes found nothing for %d s: also trying the old guessed class names and interact parts (logged once)", FALLBACK_S))
    end
    -- 2) fallback: the old guessed class names
    if fallback then
        for _, cn in ipairs(CANDIDATE_CLASSES) do
            local ok, list2 = pcall(FindAllOf, cn)
            if ok and type(list2) == "table" then
                local guess = guessEntry(kindFromName(cn))
                for _, a in ipairs(list2) do if valid(a) then add(a, guess, false) end end
            end
        end
    end
    -- 3) fallback: interact components whose type is Pickup/Treasure -> their owner actor
    local okc, comps = false, nil
    if fallback then okc, comps = pcall(FindAllOf, "PBZInteractComponent") end
    if okc and type(comps) == "table" then
        for _, c in ipairs(comps) do
            if valid(c) then
                local kind = kindFromType(readType(c))
                if kind then
                    local oko, owner = pcall(function() return c:GetOwner() end)
                    if oko and valid(owner) then add(owner, guessEntry(kind), false, nil, true) end
                end
            end
        end
        if not typeAccessor and not typeWarned and #comps > 0 then
            typeWarned = true
            log("cannot read the interact type of PBZInteractComponent (no known property/function); items come from class names only. Press F3 in game for the discovery dump (it lists the component's properties).")
        end
    end
    -- 4) only with SCAN_ACTORS on: name scan over all actors (every SCAN_S seconds); between scans the last result
    -- is kept while valid. One game call per actor (GetFullName; its first word is the class name).
    if SCAN_ACTORS and t - lastScan >= SCAN_S then
        lastScan = t
        scanItems = {}
        local seen, hit = 0, 0
        local oka, actors = pcall(FindAllOf, "Actor")
        if oka and type(actors) == "table" then
            for _, a in ipairs(actors) do
                seen = seen + 1
                local okn, id = pcall(function() return a:GetFullName() end)
                local cls = okn and type(id) == "string" and id:match("^(%S+)") or nil
                if cls and scanClassOk(cls) and valid(a) and add(a, guessEntry(kindFromName(cls)), true, id) then hit = hit + 1 end
            end
        end
        if not scanLogged then
            scanLogged = true
            log(string.format("first actor name scan done: %d actors, %d matched (FindAllOf Actor %s); next scans every %d s",
                seen, hit, (oka and type(actors) == "table") and "ok" or ("gave " .. (oka and type(actors) or tostring(actors))), SCAN_S))
        end
    else
        for id, it in pairs(scanItems) do
            if not fresh[id] then
                if valid(it.obj) then fresh[id] = it else scanItems[id] = nil end
            end
        end
    end
    -- an item that is no longer found AND no longer valid while the player was near: a "vanish" to learn from
    -- (one that is still valid was only left out of one FindAllOf answer, which happens while levels stream)
    for id, it in pairs(items) do
        if not fresh[id] and it.learn and it.snap and not valid(it.obj) then pcall(learnVanish, it, t) end
    end
    -- a door that dropped out of the list and is no longer valid: its last "vanish" check / learning step
    for id, d in pairs(doors) do
        if not freshDoors[id] and not d.dead and not valid(d.obj) then pcall(doorGone, d, t) end
    end
    items = fresh
    doors = freshDoors
    -- one log line whenever the set of class names changes (level loads, first find)
    local names = {}
    for _, it in pairs(items) do names[it.cls] = true end
    local list3 = {}
    for n in pairs(names) do list3[#list3 + 1] = n end
    table.sort(list3)
    local sig = table.concat(list3, ",")
    local nd = countKeys(doors)
    if sig ~= lastClassSig then
        lastClassSig = sig
        log("item classes now: " .. (sig == "" and "none" or sig) .. " (" .. countKeys(items) .. " items, " .. nd .. " doors)")
    end
end

-- capsule half height of the pawn = how far its location is above its feet (metres), or nil
local function readFootDz(pawn)
    local okc, cap = pcall(function() return pawn.CapsuleComponent end)
    if not okc or not valid(cap) then return nil end
    local h = UE.call(cap, "GetScaledCapsuleHalfHeight")
    if not finite(h) then h = UE.readNum(cap, "CapsuleHalfHeight") end
    if finite(h) and h > 0 and h < 500 then return h / 100 end
    return nil
end

-- every item scan: which map the player is on (a new map clears the door events and the target memory), the
-- foot offset, and tier B discovery (sense_discover decides itself when it may run; PBZ: never)
local function updateWorld(pawn, player, t)
    local full = ueFullName(pawn)
    local map = type(full) == "string" and full:match("^%S+%s+([^:]+)") or nil
    if map and mapName and map ~= mapName then
        log("map changed: " .. mapName .. " -> " .. map .. " (door events and target memory cleared)")
        doorEvents = {}
        doorTracker = state.newDoorTracker()
        pcall(state.resetBase, L)      -- first-seen values are per map (object names repeat on a reload)
        tracked, lastTargetAt = nil, -math.huge
        trackedElsewhere = nil
        if target then pcall(target.reset) end
    end
    if map then mapName = map end
    footDz = readFootDz(pawn)
    local okB, errB = pcall(discover.tierB, P, L, session, t, player, mapName)
    if not okB then logOnce("tierB", "discovery failed once (logged once): " .. tostring(errB)) end
    pcall(saveLearned)
end

-- doors: the "unlocked" value of every door is read once per DOOR_S; a locked -> unlocked flip is an event for
-- the target. Doors of guessed classes without a rule yet are watched for learning while the player is near.
local function stepDoors(t, player)
    local ctx = { ue = UE, L = L }
    for _, d in pairs(doors) do
        -- the door list is rebuilt only every 2 s, so a door may have been destroyed since. Check IsValid
        -- BEFORE any other read (doorStep, actorPos, learnSnap); an invalid door only gets doorGone (no reads).
        if d.dead then
            -- already handled; nothing is read from it until the item scan drops it
        elseif not valid(d.obj) then
            doorGone(d, t)
        else
            local rule = doorRule(d)
            if rule then
                if (d.nextRead or 0) <= t then
                    d.nextRead = t + DOOR_S
                    -- a "vanish" rule fires when the object is gone, so its position must be kept from now
                    if rule.type == "vanish" then
                        local p = UE.actorPos(d.obj)
                        if p then d.pos = p end
                    end
                    addDoorEvent(state.doorStep(doorTracker, d, t, ctx))
                end
                -- when the player was last near a "vanish" door (kept position vs the player, no game read)
                if rule.type == "vanish" and type(d.pos) == "table" then
                    local dx, dy, dz = d.pos[1] - player[1], d.pos[2] - player[2], d.pos[3] - player[3]
                    if math.sqrt(dx * dx + dy * dy + dz * dz) <= LEARN_RANGE then d.nearAt = t end
                end
            elseif not isSeed(d.entry) and (d.nextSnap or 0) <= t then
                local p = UE.actorPos(d.obj)
                if p then
                    d.pos = p      -- kept, so a door that later vanishes still has a position
                    local dx, dy, dz = p[1] - player[1], p[2] - player[2], p[3] - player[3]
                    d.dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                end
                if d.dist and d.dist <= LEARN_RANGE then learnSnap(d, t)
                else d.snap, d.nextSnap = nil, t + LEARN_S end
            end
        end
    end
end

local function readTarget(pc, pawn, player, t)
    if not target then return nil end
    if t - lastTargetAt >= TARGET_S then
        lastTargetAt = t
        local okr, tr = pcall(target.read, pc, pawn, P, TCTX)
        tracked = okr and type(tr) == "table" and tr or nil
        trackedElsewhere = nil
        if type(target.elsewhere) == "function" then
            local oke, el = pcall(target.elsewhere)
            if oke and type(el) == "table" then trackedElsewhere = el end
        end
    end
    -- the unlocked-door step always runs (its "new event" bookkeeping must see every step), but while the main
    -- quest is on another map there is no target at all ("no target" + the target_elsewhere note)
    local oku, un = pcall(target.unlockedTarget, doorEvents, player, t, TCTX)
    if trackedElsewhere then return nil end
    local okc, ch = pcall(target.choose, tracked, oku and un or nil)
    return okc and ch or nil
end

local lastErrAt = -math.huge
local function onError(text)
    local t = os.clock()
    if t - lastErrAt < ERROR_EVERY then return end
    lastErrAt = t
    log("error: " .. text)
    seq = seq + 1
    pcall(function() writeAtomic(OUT_FILE, buildMessage(seq, msNow(), nil, nil, screen, {}, text, classCounts)) end)   -- busy here = skipped, fine
end

-- the swap was refused because the reader had sense.json open: the old file stays whole, next tick retries.
-- Only a log line (at most one per 5 s), never an "error" message: that would wipe the items on the reader's side.
local lastBusyAt = -math.huge
local function onBusy(text)
    local t = os.clock()
    if t - lastBusyAt < ERROR_EVERY then return end
    lastBusyAt = t
    log("skipped one tick: " .. text)
end

-- write one message; true when the file was swapped in
local function writeMessage(text)
    local ok, err, busy = writeAtomic(OUT_FILE, text)
    if ok then return true end
    if busy then onBusy(err) else onError(err) end
    return false
end

local function writeNoPlayer()
    seq = seq + 1
    writeMessage(buildMessage(seq, msNow(), nil, nil, screen, {}, "no player yet", classCounts))
end

-- one tick, on the game thread
local function sample()
    local t = os.clock()
    if not valid(pc) then pc = findPC() end
    if not pc then writeNoPlayer(); return end
    local pawn, cam = nil, nil
    pcall(function() pawn = pc.Pawn end)
    pcall(function() cam = pc.PlayerCameraManager end)
    local player = valid(pawn) and readVec(call(pawn, "K2_GetActorLocation")) or nil
    if not player then writeNoPlayer(); return end
    local camInfo = nil
    if valid(cam) then
        local cp = readVec(call(cam, "GetCameraLocation"))
        local cr = readRot(call(cam, "GetCameraRotation"))
        local fov = call(cam, "GetFOVAngle")
        if cp and cr then camInfo = { pos = cp, rot = cr, fov = type(fov) == "number" and fov or 90.0 } end
    end
    if t - lastRebuild >= REBUILD_S then
        lastRebuild = t
        local okw, errw = pcall(updateWorld, pawn, player, t)
        if not okw then logOnce("world", "map/discovery step failed once (logged once): " .. tostring(errw)) end
        rebuild()
        screen = readScreen(pc)
    end
    local near = {}
    local counts = {}
    local chestsNear, chestsCurOn = 0, 0
    for id, it in pairs(items) do
        local drop, why = fastDrop(it)
        if drop then
            if why == "vanish" then pcall(learnVanish, it, t) end
            items[id] = nil
            scanItems[id] = nil
        else
            counts[it.cls] = (counts[it.cls] or 0) + 1
            if it.nextCheck <= t then
                local pos = readVec(call(it.obj, "K2_GetActorLocation"))
                if pos then
                    local dx, dy, dz = pos[1] - player[1], pos[2] - player[2], pos[3] - player[3]
                    it.pos, it.dist = pos, math.sqrt(dx * dx + dy * dy + dz * dz)
                    it.nextCheck = (it.dist > FAR_RANGE) and (t + 1.0) or 0
                end
            end
            -- opened/picked-up check only for items within range (the only ones that could be sent), at most once
            -- per STATE_S per item; an opened chest or a hidden item is not sent
            if it.pos and it.dist <= MAX_RANGE then
                if (it.nextState or 0) <= t then checkState(it, t) end
                if it.chest then
                    chestsNear = chestsNear + 1
                    if it.curOn then chestsCurOn = chestsCurOn + 1 end
                end
                if not it.gone then
                    near[#near + 1] = { id = id, kind = it.kind, name = it.name, pos = it.pos, dist = it.dist,
                                        state = DEBUG_STATE and it.state or nil }
                end
            end
            -- learning: only classes that are not profile seeds, only near the player
            if it.learn then
                if it.pos and it.dist <= LEARN_RANGE then
                    if (it.nextSnap or 0) <= t then pcall(learnSnap, it, t) end
                else
                    it.snap = nil
                end
            end
        end
    end
    classCounts = counts
    -- the rule "CurSwitchState true = opened" is not confirmed in the real game yet. If every chest within range
    -- reads true, that rule may be backwards (then no chest is ever sent): say so once in the log.
    if not allOpenLogged and chestsNear >= ALL_OPEN_MIN and chestsCurOn == chestsNear then
        allOpenLogged = true
        log(string.format("warning: all %d chests within %d m read CurSwitchState = true, so none is sent. If you have " ..
            "not opened them, the 'CurSwitchState true = opened' rule is wrong for this game: press F3 near a closed " ..
            "chest and check section (g) (logged once)", chestsNear, MAX_RANGE))
    end
    table.sort(near, function(a, b) return a.dist < b.dist end)
    while #near > MAX_ITEMS do near[#near] = nil end
    local okd, errd = pcall(stepDoors, t, player)
    if not okd then logOnce("doors", "door step failed once (logged once): " .. tostring(errd)) end
    local okt, tgt = pcall(readTarget, pc, pawn, player, t)
    if not okt then logOnce("target", "target step failed once (logged once): " .. tostring(tgt)); tgt = nil end
    lastTarget = tgt
    -- walking route to the target (sense_route throttles its own game calls; route_off.txt = no calls at all)
    local rt = nil
    if route and type(tgt) == "table" and type(tgt.pos) == "table" then
        local tp = tgt.pos
        if finite(tp[1]) and finite(tp[2]) and finite(tp[3]) then
            local okr, r = pcall(route.update, pawn, { X = player[1] * 100, Y = player[2] * 100, Z = player[3] * 100 },
                { X = tp[1] * 100, Y = tp[2] * 100, Z = tp[3] * 100 }, t, OUT_DIR)
            if okr then rt = type(r) == "table" and r or nil
            else logOnce("route", "route step failed once (logged once): " .. tostring(r)) end
        end
    end
    lastRoute = rt
    seq = seq + 1
    writeMessage(buildMessage(seq, msNow(), player, camInfo, screen, near, nil, classCounts, tgt or NULL, footDz, rt,
        tgt == nil and trackedElsewhere or nil))
end

-- LoopAsync clock -> one sample on the game thread; never queue a second one while the first waits
local inFlight, inFlightSince = false, 0
local function tick()
    if inFlight and os.clock() - inFlightSince < 2.0 then return false end
    inFlight, inFlightSince = true, os.clock()
    local ok, err = pcall(ExecuteInGameThread, function()
        local ok2, err2 = pcall(sample)
        inFlight = false
        if not ok2 then onError(tostring(err2)) end
    end)
    if not ok then inFlight = false; onError("ExecuteInGameThread: " .. tostring(err)) end
    return false
end

-- ---------- discovery dump (F3, once per press; FindAllOf only, never a walk over every object) ----------
local function describe(v, depth)
    depth = depth or 0
    local t = type(v)
    if t == "table" then
        if depth > 1 then return "table" end
        local parts, n = {}, 0
        for k, x in pairs(v) do
            n = n + 1
            if n > 12 then parts[#parts + 1] = "..."; break end
            parts[#parts + 1] = tostring(k) .. "=" .. describe(x, depth + 1)
        end
        return "table{" .. table.concat(parts, ", ") .. "}"
    elseif t == "userdata" then
        local s = "userdata(" .. tostring(v) .. ")"
        local ok, x, y, z = pcall(function() return v.X, v.Y, v.Z end)
        if ok and type(x) == "number" then s = s .. string.format(" X=%s Y=%s Z=%s", tostring(x), tostring(y), tostring(z)) end
        return s
    end
    return t .. "(" .. tostring(v) .. ")"
end

local propWarned = false

-- property names and types of obj's class and its parent classes (stops at engine classes); returns the names.
-- UE4SS: UStruct:ForEachProperty(callback) walks the struct's own properties (return true in the callback stops it),
-- UStruct:GetSuperStruct() gives the parent. If ForEachProperty errors it is written down once and the walk goes on.
local function propList(put, obj, indent)
    local names = {}
    local okc, cls = pcall(function() return obj:GetClass() end)
    if not okc or not valid(cls) then put(indent .. "GetClass failed: " .. tostring(cls)); return names end
    local struct, level = cls, 0
    while struct and level < MAX_SUPER do
        local okn, sname = pcall(function() return struct:GetFName():ToString() end)
        sname = okn and tostring(sname) or "?"
        if STOP_CLASSES[sname] then break end
        local props = {}
        local okf, errf = pcall(function()
            struct:ForEachProperty(function(prop)
                local okp, pn = pcall(function() return prop:GetFName():ToString() end)
                local okt, pt = pcall(function() return prop:GetClass():GetFName():ToString() end)
                pn = okp and tostring(pn) or "?"
                props[#props + 1] = pn .. ":" .. (okt and tostring(pt) or "?")
                if okp then names[#names + 1] = pn end
            end)
        end)
        if okf then
            put(string.format("%sclass %s: %d properties", indent, sname, #props))
            for i = 1, #props, 6 do put(indent .. "  " .. table.concat(props, ", ", i, math.min(i + 5, #props))) end
        else
            put(indent .. "class " .. sname .. ": ForEachProperty failed: " .. tostring(errf))
            if not propWarned then
                propWarned = true
                log("discovery: ForEachProperty failed on " .. sname .. ": " .. tostring(errf))
            end
        end
        local oks, super = pcall(function() return struct:GetSuperStruct() end)
        if oks and valid(super) then struct = super else struct = nil end
        level = level + 1
    end
    return names
end

-- values of the properties whose name looks like a type/kind/interact field (read with obj[name] in pcall)
local function propValues(put, obj, names, indent)
    local n = 0
    for _, pn in ipairs(names) do
        if anyWord(pn, PROP_WORDS) then
            n = n + 1
            local okv, v = pcall(function() return obj[pn] end)
            if okv then put(indent .. pn .. " = " .. describe(v))
            else put(indent .. pn .. " = (read failed: " .. tostring(v) .. ")") end
        end
    end
    if n == 0 then put(indent .. "(no property named like type/kind/interact)") end
end

local function ruleText(r)
    local ok, k = pcall(state.ruleKey, r)
    return ok and tostring(k) or "?"
end

local function dump()
    local t0 = os.clock()
    local out, errors = {}, 0
    local function put(s) if #out < MAX_DUMP then out[#out + 1] = s end end
    local function flush()      -- write what we have so far, so an earlier section survives whatever comes after it
        local f = io.open(DUMP_FILE, "wb")
        if not f then return false end
        f:write(table.concat(out, "\n"), "\n")
        f:close()
        return true
    end
    local function section(name, fn)
        log("discovery: " .. name)
        put("")
        put("-- " .. name)
        local ok, err = pcall(fn)
        if not ok then
            errors = errors + 1
            put("ERROR in " .. name .. ": " .. tostring(err))
            log("discovery: " .. name .. " failed: " .. tostring(err))
        end
        flush()
    end
    put("Sense discovery dump, " .. os.date("%Y-%m-%d %H:%M:%S") .. ", " .. VERSION .. ", game " .. GAME)
    put("made with FindAllOf only (no walk over every object in memory); each section is written as soon as it is done")
    put("sections: (a) actors by class name, (b) interact components, (c) property lists of the most common matched classes,")
    put("          (d) viewport/projection shapes, (e) what Sense counts right now, (f) every actor class with its count,")
    put("          (g) items within range now: sent or held back (opened/hidden), with their state values,")
    put("          (h) doors, door events, target and learned rules")
    put("matched = class name contains one of: " .. table.concat(DUMP_WORDS, ", "))
    local p = findPC()
    local pawn, player = nil, nil
    if p then
        pcall(function() pawn = p.Pawn end)
        if valid(pawn) then
            local l = locCm(pawn)
            if l then player = { l[1] / 100, l[2] / 100, l[3] / 100 } end
        end
    end
    local total, counts, matched, classActors, cdos = 0, {}, {}, {}, 0
    section("(a) actors: FindAllOf(Actor), class counts, matched actors nearest first", function()
        local oka, actors = pcall(FindAllOf, "Actor")
        if not oka then error("FindAllOf(Actor) failed: " .. tostring(actors)) end
        if type(actors) ~= "table" then put("FindAllOf(Actor) gave " .. type(actors) .. " (no actors)"); return end
        for _, a in ipairs(actors) do
            total = total + 1
            local cls = className(a)
            if cls then
                counts[cls] = (counts[cls] or 0) + 1
                if anyWord(cls, DUMP_WORDS) then
                    local full = fullName(a)
                    -- class default objects ("Default__") are not real actors in the world: their location is the
                    -- world origin and their property values are the class defaults, so they must not be the sample
                    -- actors whose InteractType the dump reads in section (c)
                    if full:find("Default__", 1, true) then
                        cdos = cdos + 1
                    else
                        local m = { cls = cls, full = full, loc = locCm(a), dist = nil }
                        if m.loc and player then
                            local dx, dy, dz = m.loc[1] / 100 - player[1], m.loc[2] / 100 - player[2], m.loc[3] / 100 - player[3]
                            m.dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                        end
                        matched[#matched + 1] = m
                        local la = classActors[cls] or {}
                        if #la < 3 then la[#la + 1] = a end
                        classActors[cls] = la
                    end
                end
            end
        end
        local mnames = {}
        for cls in pairs(classActors) do mnames[#mnames + 1] = cls end
        table.sort(mnames, byCount(counts))
        put(string.format("%d actors, %d classes, %d matched actors in %d classes (%d class default objects left out), player %s",
            total, countKeys(counts), #matched, #mnames, cdos,
            player and string.format("at %.1f,%.1f,%.1f m", player[1], player[2], player[3]) or "unknown"))
        put("matched classes (count  name  kind Sense gives it  | note):")
        for _, cls in ipairs(mnames) do
            put(string.format("%6d  %s  %s%s", counts[cls], cls, kindFromName(cls),
                scanClassOk(cls) and "" or "  | not kept by the runtime name scan"))
        end
        table.sort(matched, function(a, b)
            local da, db = a.dist or math.huge, b.dist or math.huge
            if da ~= db then return da < db end
            return a.full < b.full
        end)
        put("nearest " .. math.min(200, #matched) .. " matched actors (dist m | class | full name | loc cm):")
        for i = 1, math.min(200, #matched) do
            local m = matched[i]
            put(string.format("%s | %s | %s | loc=%s", m.dist and string.format("%7.1f", m.dist) or "      ?",
                m.cls, m.full, m.loc and fmtLoc(m.loc) or "?"))
        end
    end)
    section("(b) interact components: first 5 of PBZInteractComponent and PBZInteractRegion (owner, property list, type-like values)", function()
        for _, cn in ipairs({ "PBZInteractComponent", "PBZInteractRegion" }) do
            local ok, list = pcall(FindAllOf, cn)
            if not ok then
                put(cn .. ": FindAllOf failed: " .. tostring(list))
            elseif type(list) ~= "table" then
                put(cn .. ": none found (FindAllOf gave " .. type(list) .. ")")
            else
                put(string.format("%s: %d instances", cn, #list))
                local names = nil
                for i = 1, math.min(5, #list) do
                    local c = list[i]
                    local oki, erri = pcall(function()
                        put(string.format("  #%d %s", i, fullName(c)))
                        local oko, owner = pcall(function() return c:GetOwner() end)
                        if oko and valid(owner) then
                            put(string.format("    owner: %s | %s | loc=%s", className(owner) or "?", fullName(owner), locStr(owner)))
                        else
                            put("    owner: none")
                        end
                        if not names then names = propList(put, c, "    ") end
                        propValues(put, c, names, "    ")
                    end)
                    if not oki then put("    ERROR: " .. tostring(erri)) end
                end
            end
        end
    end)
    section("(c) property lists: first 3 actors of the 5 most common matched classes", function()
        local mnames = {}
        for cls in pairs(classActors) do mnames[#mnames + 1] = cls end
        table.sort(mnames, byCount(counts))
        if #mnames == 0 then put("no matched actors (see section (a))") end
        for i = 1, math.min(5, #mnames) do
            local cls = mnames[i]
            put(string.format("class %s (%d actors)", cls, counts[cls]))
            local names = nil
            for j, a in ipairs(classActors[cls]) do
                local okj, errj = pcall(function()
                    put(string.format("  actor %d: %s | loc=%s", j, fullName(a), locStr(a)))
                    if not names then names = propList(put, a, "    ") end
                    propValues(put, a, names, "    ")
                end)
                if not okj then put("  ERROR: " .. tostring(errj)) end
            end
        end
    end)
    section("(d) player, viewport and projection shapes", function()
        if not p then put("no PlayerController yet"); return end
        local loc = call(pawn, "K2_GetActorLocation")
        put("PlayerController: " .. tostring(call(p, "GetFullName")))
        put("Pawn: " .. tostring(call(pawn, "GetFullName")) .. " loc=" .. describe(loc))
        local ox, oy = {}, {}
        local r = { pcall(function() return p:GetViewportSize(ox, oy) end) }
        put("GetViewportSize: ok=" .. tostring(r[1]) .. " returns=" .. describe({ table.unpack(r, 2) })
            .. " outX=" .. describe(ox) .. " outY=" .. describe(oy) .. " -> screen used: " .. readScreen(p)[1] .. "x" .. readScreen(p)[2])
        local okr, res = pcall(function() return UEHelpers.GetEngine().GameUserSettings:GetScreenResolution() end)
        put("GameUserSettings:GetScreenResolution: ok=" .. tostring(okr) .. " -> " .. describe(res))
        local scr = {}
        local r2 = { pcall(function() return p:ProjectWorldLocationToScreen(loc, scr, true) end) }
        put("ProjectWorldLocationToScreen(player loc): ok=" .. tostring(r2[1]) .. " returns="
            .. describe({ table.unpack(r2, 2) }) .. " out=" .. describe(scr))
        local cam = nil
        pcall(function() cam = p.PlayerCameraManager end)
        put("camera: loc=" .. describe(call(cam, "GetCameraLocation")) .. " rot=" .. describe(call(cam, "GetCameraRotation"))
            .. " fov=" .. tostring(call(cam, "GetFOVAngle")))
        put("foot offset (capsule half height): " .. (footDz and string.format("%.2f m", footDz) or "unknown"))
    end)
    section("(e) what Sense counts right now", function()
        put("game " .. GAME .. " (from " .. GAME_HOW .. "), profile: default " .. tostring(PINFO and PINFO.default)
            .. ", game file " .. tostring(PINFO and PINFO.game) .. ((PINFO and PINFO.builtin) and " (built-in profile used)" or "") .. ", " .. #(P.seeds or {}) .. " seeds, "
            .. #(P.doors or {}) .. " door classes, discovery " .. tostring(P.discovery and P.discovery.mode))
        put("interact type accessor: " .. (typeAccessor and (typeAccessor.how .. " " .. typeAccessor.name) or "none found"))
        local cnames = {}
        for n in pairs(classCounts) do cnames[#cnames + 1] = n end
        table.sort(cnames, byCount(classCounts))
        put("classes in the cache (count  name):" .. (#cnames == 0 and " none" or ""))
        for _, n in ipairs(cnames) do put(string.format("%6d  %s", classCounts[n], n)) end
        local snames = {}
        for n, e in pairs(type(session.classes) == "table" and session.classes or {}) do
            snames[#snames + 1] = n .. " (" .. tostring(e.kind) .. ", " .. tostring(e.reason) .. ")"
        end
        table.sort(snames)
        put("classes guessed this session: " .. (#snames == 0 and "none" or table.concat(snames, ", ")))
        put("items (id | kind | dist m):")
        local n = 0
        for id, it in pairs(items) do
            n = n + 1
            if n <= 200 then put(id .. " | " .. it.kind .. " | " .. (it.dist == math.huge and "?" or string.format("%.1f", it.dist))) end
        end
        put("(" .. n .. " cached items)")
    end)
    section("(f) every actor class with its count", function()
        local names = {}
        for n in pairs(counts) do names[#names + 1] = n end
        table.sort(names, byCount(counts))
        for _, n in ipairs(names) do put(string.format("%6d  %s", counts[n], n)) end
    end)
    section("(g) items Sense sends now, with their state values (read fresh; press F3 before and after opening a chest)", function()
        put(string.format("cached items within %d m (the only ones whose state Sense reads); nearest first", MAX_RANGE))
        put("SENT / HELD BACK | dist m | kind | class | CurSwitchState bInteracted_n bInteracted_true startInteract hidden hasDestory loot | id")
        local list = {}
        for id, it in pairs(items) do
            if it.pos and it.dist <= MAX_RANGE then list[#list + 1] = { id = id, it = it } end
        end
        table.sort(list, function(a, b) return a.it.dist < b.it.dist end)
        local tNow = os.clock()
        local function sv(v) if v == nil or v == NULL then return "-" end return tostring(v) end
        for i, e in ipairs(list) do
            if i > 200 then put("... (" .. #list .. " in all)"); break end
            local it = e.it
            -- never read a destroyed actor (the item may be gone since the last IsValid in sample())
            local alive = valid(it.obj)
            if alive then
                local okc, errc = pcall(checkState, it, tNow)
                if not okc then put("  state read failed: " .. tostring(errc)) end
            else
                put("  (gone: no longer valid, not read)")
            end
            local s = (alive and it.state) or {}
            put(string.format("%s | %6.1f | %s | %s | %s %s %s %s %s %s %s | %s", it.gone and "HELD BACK" or "SENT     ",
                it.dist, it.kind, it.cls, sv(s.CurSwitchState), sv(s.bInteracted_n), sv(s.bInteracted_true),
                sv(s.startInteract), sv(s.hidden), sv(s.hasDestory), sv(s.loot), e.id))
        end
        put("(" .. #list .. " items within range" .. (DEBUG_STATE and "" or "; DEBUG_STATE is off, so no state values") .. ")")
    end)
    section("(h) doors, door events, target and learned rules", function()
        local dl = {}
        for id, d in pairs(doors) do dl[#dl + 1] = { id = id, d = d } end
        table.sort(dl, function(a, b) return a.id < b.id end)
        put(#dl .. " doors cached (class | unlocked rule | last read | id):")
        for i, e in ipairs(dl) do
            if i > 100 then put("... (" .. #dl .. " in all)"); break end
            local d = e.d
            local rule = (type(d.entry) == "table" and d.entry.unlocked) or (type(L.doors) == "table" and type(L.doors[d.cls]) == "table" and L.doors[d.cls].unlocked)
            local last = nil   -- true / false (a plain "x and v or nil" would turn false into nil)
            if type(doorTracker.last) == "table" then last = doorTracker.last[e.id] end
            local lastText = "-"
            if last ~= nil then lastText = last and "unlocked" or "locked" end
            put(string.format("  %s | %s | %s | %s", tostring(d.cls), rule and ruleText(rule) or "none yet (learning)",
                lastText, e.id))
        end
        put(#doorEvents .. " door unlocked events this session (oldest first):")
        for _, ev in ipairs(doorEvents) do
            put(string.format("  %s %s at %.1f,%.1f,%.1f m, t=%.1f%s", tostring(ev.kind), tostring(ev.id), ev.pos[1], ev.pos[2],
                ev.pos[3], ev.t or 0, ev.reached and " (reached)" or ""))
        end
        local tg = lastTarget
        if type(tg) == "table" and type(tg.pos) == "table" then
            put(string.format("target now: %s (%s) %s at %.1f,%.1f,%.1f m %s", tostring(tg.kind), tostring(tg.src), tostring(tg.id),
                tg.pos[1] or 0, tg.pos[2] or 0, tg.pos[3] or 0, tg.label ~= "" and ("'" .. tostring(tg.label) .. "'") or ""))
        else
            put("target now: none (profile target " .. tostring(P.target and P.target.type) .. ")")
        end
        if type(trackedElsewhere) == "table" then
            put("main quest elsewhere: '" .. tostring(trackedElsewhere.label) .. "' is on map " ..
                tostring(trackedElsewhere.map) .. " (no target, target_elsewhere sent)")
        end
        if not route then
            put("walking route: sense_route.lua not loaded (straight line)")
        else
            local okS, RS = pcall(route._state)
            RS = okS and type(RS) == "table" and RS or {}
            local lr = lastRoute
            put(string.format("walking route: %s%s%s%s, %s rounds, last %.2f ms%s", type(lr) == "table" and
                (tostring(lr.src) .. " with " .. #(lr.pts or {}) .. " points") or "none",
                RS.killOn and " (route_off.txt: OFF)" or "", RS.off and " (switched off after errors)" or "",
                RS.backoff and " (slow: one round per 5 s)" or "", tostring(RS.rounds or 0), tonumber(RS.lastMs) or 0,
                RS.lastWhy and (", last miss: " .. tostring(RS.lastWhy)) or ""))
        end
        local ln = {}
        for cls, e in pairs(type(L.classes) == "table" and L.classes or {}) do
            local rs = {}
            for _, r in ipairs(type(e.gone) == "table" and e.gone or {}) do rs[#rs + 1] = ruleText(r) end
            ln[#ln + 1] = string.format("  item %s (%s): taken when %s", cls, tostring(e.kind), table.concat(rs, " or "))
        end
        for cls, e in pairs(type(L.doors) == "table" and L.doors or {}) do
            ln[#ln + 1] = string.format("  door %s: unlocked when %s", cls, ruleText(e.unlocked))
        end
        table.sort(ln)
        put(#ln .. " learned rules for " .. GAME .. ":")
        for _, s in ipairs(ln) do put(s) end
    end)
    local okw = flush()
    log(string.format("discovery: %d actors, %d classes, %d matched, %d section error(s), %.1f s, %d lines -> %s%s",
        total, countKeys(counts), #matched, errors, os.clock() - t0, #out, DUMP_FILE, okw and "" or " (COULD NOT WRITE THE FILE)"))
end

local discovering, discoveringSince = false, 0
local function discover_()
    if discovering then
        -- normally the queued game-thread callback clears this flag; if that callback never ran (dropped on a level
        -- change, or the engine tick path changed), F3 must not stay dead for the rest of the session
        if os.clock() - discoveringSince < DISCOVER_TIMEOUT_S then
            log("discovery: a dump is still running, this F3 press is ignored")
            return
        end
        log(string.format("discovery: the last dump never reported back in %d s, starting a new one", DISCOVER_TIMEOUT_S))
    end
    discovering, discoveringSince = true, os.clock()
    log("discovery dump started (F3)...")
    local ok, err = pcall(ExecuteInGameThread, function()
        local ok2, err2 = pcall(dump)
        discovering = false
        if not ok2 then log("discovery failed: " .. tostring(err2)) end
    end)
    if not ok then discovering = false; log("discovery failed: " .. tostring(err)) end
end

-- ---------- start ----------
local function start()
    LoopAsync(TICK_MS, tick)
    local ok, err = pcall(function() RegisterKeyBind(Key.F3, discover_) end)
    if ok then log("F3 = discovery dump (once per press)") else log("F3 keybind failed: " .. tostring(err)) end
end

if not READY then
    log("could not start: missing module(s) " .. table.concat(loadProblems, "; ") .. " (the sense_*.lua files must sit next to main.lua)")
else
    if #loadProblems > 0 then log("running without: " .. table.concat(loadProblems, "; ")) end
    if route then pcall(route.setLog, log) end
    local okStart, errStart = pcall(start)
    if okStart then
        log("loaded (read-only). Game " .. GAME .. " (" .. GAME_HOW .. "), " .. #(P.seeds or {}) .. " item classes, "
            .. #(P.doors or {}) .. " door classes, discovery " .. tostring(P.discovery and P.discovery.mode) .. ", target "
            .. tostring(P.target and P.target.type) .. ", " .. learnedN .. " learned. Writing " .. OUT_FILE .. " every "
            .. TICK_MS .. " ms (folder found via " .. OUT_HOW .. ")")
    else
        log("could not start: " .. tostring(errStart))
    end
end

-- for offline tests (UE4SS ignores this return value)
return {
    toJson = toJson, array = array, NULL = NULL, buildMessage = buildMessage, writeAtomic = writeAtomic,
    kindFromType = kindFromType, kindFromName = kindFromName, scanClassOk = scanClassOk, outNum = outNum,
    readScreen = readScreen, modDir = modDir, sample = sample, rebuild = rebuild, dump = dump, tick = tick,
    propList = propList, propValues = propValues, OUT_FILE = OUT_FILE, DUMP_FILE = DUMP_FILE,
    MAX_RANGE = MAX_RANGE, VERSION = VERSION, SCAN_S = SCAN_S, SCAN_IGNORE = SCAN_IGNORE,
    DISCOVER_TIMEOUT_S = DISCOVER_TIMEOUT_S, ITEM_CLASSES = ITEM_CLASSES, NOT_ITEMS = NOT_ITEMS, notItem = notItem,
    SCAN_ACTORS = SCAN_ACTORS, DEBUG_STATE = DEBUG_STATE, FALLBACK_S = FALLBACK_S, STATE_S = STATE_S,
    -- engine-helper additions
    READY = READY, GAME = GAME, P = P, L = L, session = session, LEARN_RANGE = LEARN_RANGE,
    routeMod = route, routeJson = routeJson,
    peek = function()
        return { items = items, doors = doors, doorEvents = doorEvents, tracked = tracked, target = lastTarget,
                 elsewhere = trackedElsewhere,
                 route = lastRoute,
                 mapName = mapName, footDz = footDz, learnedN = learnedN, learnedDirty = learnedDirty }
    end,
}
