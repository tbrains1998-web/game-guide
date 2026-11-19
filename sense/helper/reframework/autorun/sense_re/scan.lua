-- sense_re/scan.lua - finds item / chest / door objects for the RE Engine Sense helper. Read-only.
-- Loaded with require("sense_re.scan") by autorun/sense_re.lua; M.tick(ctx) runs 10 times a second.
--
-- How it finds things:
--   1. Search types (every 2 s, every 1 s when something is within 20 m): profile seeds, profile doors, profile
--      base_types + seed parents, learned classes/doors, and the types the sweep found this session. For each one:
--      scene:findComponents(System.Type) -> component -> GameObject -> Transform -> position. Nearest 300 kept.
--   2. Generic sweep (discovery, once per area load): scene:findComponents(via.Transform), only objects within
--      3 x range of the player, each GameObject's components, type full name + parent types matched against the
--      keyword lists (door > collectible > item, exclude words, not_items). Types that match become search types.
--      The sweep is spread over many ticks (time budget per tick); a sweep tick fetches the array ONCE and walks
--      one slice of it, then continues at a number offset next time, so no game object is ever kept from one tick
--      to the next. The walk budget starts after the fetch; when the fetch itself is slow, the
--      walk gets at least as much time as the fetch and the next sweep tick waits, so the sweep averages about
--      SWEEP_CHUNK_S per 0.1 s tick.
-- No game object or address is kept across ticks: every read happens in the same tick as the findComponents that
-- returned the object. Records handed to learn/out are plain Lua data. State reads (snapshot, gone, require,
-- door unlocked) are done right here, in that same tick, through learn.observe(rec, go, comp, P).
--
-- Public API:
--   M.init(ctx)                    reset session state
--   M.tick(ctx)                    reads ctx.profile, ctx.player {x,y,z}, ctx.now, ctx.area (optional);
--                                  sets ctx.records, ctx.classes, ctx.scan_info (see below)
--   M.scan(P, world, now)          -> records, info   (what tick does; world = {player={x,y,z}|{pos=..}, area=?,
--                                     scene=fn|obj}; P = merged profile)
--   M.classify(chain, P, L, words) -> kind ("pickup"|"collectible"|"door") | nil, reason, entry
--                                     chain = {own type full name, parent full names...}; words=false: no keyword guess
--   M.tokens(name) / M.hasWord(tokens, word)   keyword matching helpers (camel-case tokens, see below)
--   M.reset()                      forget everything (area cache, sweep state, found types, ids)
--   M.stats()                      counters for the log / tests
-- record (plain data): { id="GoName|app.Type"[ "@x,z"], kind="pickup"|"collectible"|"door", name="Type short name",
--   tname="app.Type", go_name="GoName", src="seed"|"door"|"learned"|"sweep"|"base", search="type searched",
--   pos={x,y,z}, dist=m (at refresh), obs=serial of the refresh that read it, t=now at that refresh,
--   plus what learn.observe adds: gone=bool, why=text, req=bool, unlocked=true|false|nil (doors), st=snapshot|nil }
-- info: { serial=n (bumps on every refresh), refreshed={search type=true} (types read completely this refresh),
--   partial=bool, area_changed=bool, sweeping=bool, sweeps=n, found=n, scan_s=secs, changed=bool (refresh now) }

local C = require("sense_re.core")
local Lrn = nil   -- sense_re.learn (loaded lazily: the loader loads scan before learn)

local M = {}

M.MAX_RECORDS = 300          -- nearest kept
M.PER_TYPE_CAP = 2000        -- components read per search type per refresh
M.KEEP_RADIUS = 250          -- metres; records farther than this are not kept
M.REFRESH_S = 2.0
M.REFRESH_NEAR_S = 1.0       -- when a record is within NEAR_REFRESH_M (learning needs finer steps)
M.NEAR_REFRESH_M = 20
M.REFRESH_BUDGET_S = 0.006   -- per refresh; the rest of the types is read at the next refresh (rotation)
M.REFRESH_HARD_S = 0.010     -- inside one big type: stop here and go on at the same element next refresh
M.SWEEP_CHUNK_S = 0.004      -- per tick while a sweep runs
M.SWEEP_CHUNK_MAX = 1500     -- transforms per tick (also bounds the work when the clock does not move)
M.SWEEP_WALK_MAX_S = 0.012   -- the walk may take as long as the array fetch did, but never longer than this
M.SWEEP_WAIT_MAX_S = 2.0     -- longest pause between two sweep ticks after a slow fetch
M.SWEEP_RADIUS_MULT = 3      -- sweep keeps objects within 3 x range
M.SWEEP_MAX_S = 60           -- a sweep that takes longer (wall) is stopped
M.COMP_CAP = 32              -- components per GameObject in the sweep
M.FOUND_CAP = 200            -- types the sweep may add
M.AREA_MIN_S = 10            -- an area change / teleport starts a sweep at most this often
M.CARRY_MAX_S = 30           -- records of a type that could not be read are kept at most this long
M.RANGE = 30

local huge, floor, sqrt = math.huge, math.floor, math.sqrt

-- built-in word lists (used only when the profile has none; profile.lua normally merges _default.json)
M.DEFAULT_WORDS = {
    item_words = { "Item", "Pickup", "Loot", "Drop" },
    collectible_words = { "Chest", "TreasureBox", "Treasure", "Safe", "Locker", "Collect" },
    door_words = { "Door", "Gate", "Shutter", "Lock", "Barricade" },
    exclude_words = { "Param", "Data", "Setting", "Manager", "Gui", "Sound", "Effect", "Driver", "BehaviorTree",
                      "Track", "Clock", "LockOn", "Catalog", "Icon",
                      "Character", "Human", "Npc", "Enemy" },   -- parts on moving characters
}

---------------------------------------------------------------------------------------------------------------------
-- small helpers
---------------------------------------------------------------------------------------------------------------------
local function learnMod()
    if Lrn == nil then
        local ok, m = pcall(require, "sense_re.learn")
        Lrn = (ok and type(m) == "table") and m or false
    end
    return Lrn or nil
end

local function clock() return C.clock() end

local function dist(a, b)
    local dx, dy, dz = a[1] - b[1], a[2] - b[2], a[3] - b[3]
    return sqrt(dx * dx + dy * dy + dz * dz)
end

local function shortName(t)
    t = tostring(t or "")
    t = t:gsub("[<`].*$", "")
    return t:match("([^%.]+)$") or t
end

-- player position from ctx.player / world: {x,y,z} | {x=,y=,z=} | {pos=...}
local function posOf(v)
    if type(v) ~= "table" then return nil end
    if v[1] == nil and v.x == nil and type(v.pos) == "table" then v = v.pos end
    local x, y, z = v[1], v[2], v[3]
    if x == nil then x, y, z = v.x, v.y, v.z end
    x, y, z = C.num(x), C.num(y), C.num(z)
    if x and y and z then return { x, y, z } end
    return nil
end

---------------------------------------------------------------------------------------------------------------------
-- keyword matching: names are split into lower-case tokens at '.', '_', camel case and digits
--   "app.GmDoubleDoorV3" -> app gm double door v 3 ; "IGimmickGate" -> i gimmick gate ; "RDLDDoorOpen" -> rdld door open
-- A word matches one or more whole tokens in a row (joined): "TreasureBox" = treasure+box; a token may add "s"/"es";
-- a word of 5+ letters may also be the start of a token ("collect" -> "collectible"). So "Lock" does not match
-- "Clock", "Block" or "Locker"; "Gate" does not match "Navigate".
---------------------------------------------------------------------------------------------------------------------
local tokCache = {}
local tokCacheN = 0

function M.tokens(s)
    s = tostring(s or "")
    local c = tokCache[s]
    if c then return c end
    local out = {}
    for part in s:gmatch("[%w]+") do
        local cur = ""
        local n = #part
        for i = 1, n do
            local ch = part:sub(i, i)
            local prev = (i > 1) and part:sub(i - 1, i - 1) or ""
            local nxt = (i < n) and part:sub(i + 1, i + 1) or ""
            if cur ~= "" then
                local split = false
                if ch:match("%u") and (prev:match("%l") or prev:match("%d")) then split = true
                elseif ch:match("%u") and prev:match("%u") and nxt:match("%l") then split = true
                elseif ch:match("%d") and not prev:match("%d") then split = true
                elseif ch:match("%a") and prev:match("%d") then split = true end
                if split then out[#out + 1] = cur:lower(); cur = "" end
            end
            cur = cur .. ch
        end
        if cur ~= "" then out[#out + 1] = cur:lower() end
    end
    if tokCacheN > 5000 then tokCache, tokCacheN = {}, 0 end
    tokCache[s] = out
    tokCacheN = tokCacheN + 1
    return out
end

local function tokMatch(tok, w)
    if tok == w or tok == w .. "s" or tok == w .. "es" then return true end
    return #w >= 5 and tok:sub(1, #w) == w
end

-- w: a word (any case); toks: tokens of a name
function M.hasWord(toks, w)
    w = tostring(w or ""):lower():gsub("[^%w]", "")
    if w == "" then return false end
    for i = 1, #toks do
        local s = ""
        for j = i, #toks do
            s = s .. toks[j]
            if tokMatch(s, w) then return true end
            if #s >= #w then break end
        end
    end
    return false
end

local function anyWord(toks, list)
    if type(list) ~= "table" then return nil end
    for i = 1, #list do
        local w = list[i]
        if type(w) == "string" and M.hasWord(toks, w) then return w end
    end
    return nil
end

---------------------------------------------------------------------------------------------------------------------
-- profile / learned lookups
---------------------------------------------------------------------------------------------------------------------
local function words(P, key)
    local l = type(P) == "table" and P[key] or nil
    if type(l) == "table" then return l end
    return M.DEFAULT_WORDS[key]
end

local function notItem(P, name)
    if type(P) ~= "table" or not name then return false end
    local set = P.not_items_set
    if type(set) == "table" and (set[name] or set[shortName(name)]) then return true end
    if type(set) ~= "table" and type(P.not_items) == "table" then
        local sh = shortName(name)
        for _, n in ipairs(P.not_items) do if n == name or n == sh then return true end end
    end
    return false
end

local function seedBy(P, name)
    if type(P) ~= "table" or not name then return nil end
    if type(P.seed_by_name) == "table" then return P.seed_by_name[name] end
    for _, s in ipairs(type(P.seeds) == "table" and P.seeds or {}) do
        if type(s) == "table" and s.name == name then return s end
    end
    return nil
end

local function doorBy(P, name)
    if type(P) ~= "table" or not name then return nil end
    if type(P.door_by_name) == "table" then return P.door_by_name[name] end
    for _, d in ipairs(type(P.doors) == "table" and P.doors or {}) do
        if type(d) == "table" and d.name == name then return d end
    end
    return nil
end

local KINDS = { pickup = true, collectible = true }

local function learnedClass(L, name)
    local c = type(L) == "table" and type(L.classes) == "table" and L.classes[name] or nil
    if type(c) == "table" then return c end
    return nil
end

local function learnedDoor(L, name)
    local d = type(L) == "table" and type(L.doors) == "table" and L.doors[name] or nil
    if type(d) == "table" then return d end
    return nil
end

local function wordsAllowed(P)
    local d = type(P) == "table" and P.discovery or nil
    return not (type(d) == "table" and d.mode == "off")
end

function M.classify(chain, P, L, allowWords)
    if type(chain) ~= "table" or type(chain[1]) ~= "string" or chain[1] == "" then return nil, "no type" end
    local own = chain[1]
    if notItem(P, own) then return nil, "not_items" end
    if learnedDoor(L, own) then return "door", "learned" end
    local lc = learnedClass(L, own)
    if lc then return KINDS[lc.kind] and lc.kind or "pickup", "learned" end
    local s = seedBy(P, own)
    if s and KINDS[s.kind] then return s.kind, "seed", s end
    local d = doorBy(P, own)
    if d then return "door", "door", d end
    for i = 2, #chain do
        local pn = chain[i]
        local ps = seedBy(P, pn)
        if ps and KINDS[ps.kind] then return ps.kind, "seed parent " .. pn, ps end
        local pd = doorBy(P, pn)
        if pd then return "door", "door parent " .. pn, pd end
    end
    if allowWords == false then return nil, "no words" end
    local exw = words(P, "exclude_words")
    local x = anyWord(M.tokens(own), exw)
    if x then return nil, "exclude " .. x end
    local function kw(name)
        local toks = M.tokens(shortName(name))
        local w = anyWord(toks, words(P, "door_words"))
        if w then return "door", w end
        w = anyWord(toks, words(P, "collectible_words"))
        if w then return "collectible", w end
        w = anyWord(toks, words(P, "item_words"))
        if w then return "pickup", w end
        return nil
    end
    local k, w = kw(own)
    if k then return k, "word " .. w end
    for i = 2, #chain do
        local pn = chain[i]
        if not anyWord(M.tokens(pn), exw) and not notItem(P, pn) then
            k, w = kw(pn)
            if k then return k, "word " .. w .. " in parent " .. pn end
        end
    end
    return nil, "no word"
end

---------------------------------------------------------------------------------------------------------------------
-- session state
---------------------------------------------------------------------------------------------------------------------
local S = {}

local function newState()
    S = {
        records = {},          -- last list handed out (plain)
        serial = 0,
        nextRefresh = -huge,
        rot = 0,               -- rotation start for partial refreshes
        missing = {},          -- type name -> true (sdk.typeof gave nothing; retried on area change / sweep)
        elemOff = {},          -- type name -> element index to go on at (a big type read over several refreshes)
        winOff = {},           -- type name -> start of the PER_TYPE_CAP window (lists longer than the cap move it)
        found = {},            -- type name -> {kind=, reason=, entry=} (sweep; entry = seed/door entry of a parent)
        foundList = {},
        foundFull = false,
        sweep = nil,           -- {offset, started, center, spent, seen={tname=kind|false}, n=0, hits=0}
        lastSweepT = nil, lastSweepCenter = nil, lastArea = nil, lastPos = nil, backoff = 1,
        sweeps = 0, lastSweepSpent = 0, lastSweepWall = 0,
        refreshes = 0, lastScanS = 0, partial = false, areaChanged = false,
        idsPrev = {},          -- base -> list of {id=, pos=}
        logged = {},
    }
end
newState()

local function logOnce(key, msg)
    if S.logged[key] then return end
    S.logged[key] = true
    pcall(C.log, "scan: " .. msg)
end

function M.reset() newState() end
function M.init(ctx) newState() end

function M.stats()
    return { refreshes = S.refreshes, serial = S.serial, sweeps = S.sweeps, found = #S.foundList,
             sweeping = S.sweep ~= nil, records = #S.records, last_scan_s = S.lastScanS,
             last_sweep_s = S.lastSweepSpent, missing = (function() local n = 0
                 for _ in pairs(S.missing) do n = n + 1 end return n end)() }
end

function M.foundTypes()
    local out = {}
    for i, n in ipairs(S.foundList) do out[i] = { name = n, kind = S.found[n].kind, reason = S.found[n].reason } end
    return out
end

---------------------------------------------------------------------------------------------------------------------
-- game access (all through core probes; nothing kept after the tick)
---------------------------------------------------------------------------------------------------------------------
local function getScene(world)
    local sc = world and world.scene
    if type(sc) == "function" then
        local ok, r = pcall(sc)
        return ok and r or nil
    end
    if sc ~= nil then return sc end
    local W = package.loaded["sense_re.world"]
    if type(W) == "table" and type(W.scene) == "function" then
        local ok, r = pcall(W.scene)
        if ok then return r end
        return nil
    end
    if not (sdk and sdk.get_native_singleton and sdk.call_native_func) then return nil end
    local okS, sm = pcall(sdk.get_native_singleton, "via.SceneManager")
    local td = C.tdef("via.SceneManager")
    if not okS or sm == nil or td == nil then return nil end
    local ok, r = pcall(sdk.call_native_func, sm, td, "get_CurrentScene()")
    return ok and r or nil
end

local function typeOf(name)
    if not (sdk and sdk.typeof) then return nil end
    local ok, t = pcall(sdk.typeof, name)
    if ok then return t end
    return nil
end

-- managed array -> size, getter(i 0-based); also plain Lua lists
local function arrayAccess(arr, cap)
    if arr == nil then return nil end
    if type(arr) == "table" and getmetatable(arr) == nil then
        return math.min(#arr, cap), function(i) return arr[i + 1] end, #arr
    end
    local okS, n = pcall(function() return arr:get_size() end)
    if okS and type(n) == "number" then
        return math.min(n, cap), function(i)
            local ok, e = pcall(function() return arr:get_element(i) end)
            if ok then return e end
            return nil
        end, n
    end
    local list = C.elements(arr, cap)
    return #list, function(i) return list[i + 1] end, #list
end

local function findAll(scene, tname)
    local t = typeOf(tname)
    if t == nil then return nil, "notype" end
    local arr = C.pcallm(scene, "findComponents(System.Type)", t)
    if arr == nil then return nil, "noarray" end
    return arr
end

-- metres through the shared C.toM (same scale as the player)
local function goPos(go, P)
    local tr = C.pcallm(go, "get_Transform")
    if tr == nil then return nil end
    return C.toM(C.pcallm(tr, "get_Position"), P)
end

---------------------------------------------------------------------------------------------------------------------
-- search list
---------------------------------------------------------------------------------------------------------------------
local function searchList(P, L)
    local list, seen = {}, {}
    local function add(name, src, kind, entry)
        if type(name) ~= "string" or name == "" or seen[name] then return end
        seen[name] = true
        list[#list + 1] = { name = name, src = src, kind = kind, entry = entry }
    end
    if type(P) == "table" then
        for _, s in ipairs(type(P.seeds) == "table" and P.seeds or {}) do
            if type(s) == "table" and KINDS[s.kind] and not notItem(P, s.name) then add(s.name, "seed", s.kind, s) end
        end
        for _, d in ipairs(type(P.doors) == "table" and P.doors or {}) do
            if type(d) == "table" then add(d.name, "door", "door", d) end
        end
    end
    if type(L) == "table" then
        local names = {}
        for n in pairs(type(L.classes) == "table" and L.classes or {}) do names[#names + 1] = n end
        table.sort(names)
        for _, n in ipairs(names) do
            local c = L.classes[n]
            if type(c) == "table" and not notItem(P, n) then add(n, "learned", KINDS[c.kind] and c.kind or "pickup") end
        end
        names = {}
        for n in pairs(type(L.doors) == "table" and L.doors or {}) do names[#names + 1] = n end
        table.sort(names)
        for _, n in ipairs(names) do add(n, "learned", "door") end
    end
    for _, n in ipairs(S.foundList) do
        local f = S.found[n]
        if f and not notItem(P, n) then add(n, "sweep", f.kind, f.entry) end   -- entry: seed parent's rules (#8)
    end
    if type(P) == "table" then
        -- base types and seed parents: every component found there is classified by its own type
        for _, b in ipairs(type(P.base_types) == "table" and P.base_types or {}) do add(b, "base", nil) end
        for _, s in ipairs(type(P.seeds) == "table" and P.seeds or {}) do
            if type(s) == "table" and type(s.parent) == "string" then add(s.parent, "base", nil) end
        end
    end
    return list
end

---------------------------------------------------------------------------------------------------------------------
-- ids: "GoName|app.Type"; a second object with the same base gets "@x,z" (rounded metres at first sight).
-- An object keeps its id across refreshes when it is within 1.5 m of where a same-base record was last time.
---------------------------------------------------------------------------------------------------------------------
local function assignIds(list)
    local prev = S.idsPrev
    local used, claimed = {}, {}
    local nextPrev = {}
    -- first pass: reuse ids by proximity
    for _, r in ipairs(list) do
        local cands = prev[r.base]
        if cands then
            local best, bestD = nil, 1.5
            for _, c in ipairs(cands) do
                if not claimed[c.id] then
                    local d = dist(c.pos, r.pos)
                    if d <= bestD then best, bestD = c, d end
                end
            end
            if best then r.id = best.id; claimed[best.id] = true; used[best.id] = true end
        end
    end
    -- reserve the bare base of every previous record (so a newcomer does not take a still-known id)
    local reserved = {}
    for base, cands in pairs(prev) do for _, c in ipairs(cands) do reserved[c.id] = true end end
    for _, r in ipairs(list) do
        if not r.id then
            local id = r.base
            if used[id] or reserved[id] then
                id = string.format("%s@%d,%d", r.base, floor(r.pos[1] + 0.5), floor(r.pos[3] + 0.5))
                local k = 2
                local try = id
                while used[try] or reserved[try] do try = id .. "~" .. k; k = k + 1 end
                id = try
            end
            r.id = id
            used[id] = true
        end
        local l = nextPrev[r.base]
        if not l then l = {}; nextPrev[r.base] = l end
        l[#l + 1] = { id = r.id, pos = r.pos }
    end
    S.idsPrev = nextPrev
end

---------------------------------------------------------------------------------------------------------------------
-- refresh: read every search type, keep the nearest, read their state (same tick), hand out plain records
---------------------------------------------------------------------------------------------------------------------
local function refresh(P, L, scene, ppos, now)
    local t0 = clock()
    local list = searchList(P, L)
    local n = #list
    local raw, byKey = {}, {}
    local refreshed = {}
    local partial = false
    local allowWords = wordsAllowed(P)
    local kindCache = {}       -- type name -> {kind, entry} | false, for components found through base types
    local start = (n > 0) and (S.rot % n) or 0
    local nextRot = 0
    local readTypes = 0
    for k = 0, n - 1 do
        local idx = (start + k) % n
        if k > 0 and clock() - t0 > M.REFRESH_BUDGET_S then
            partial, nextRot = true, idx
            break
        end
        local s = list[idx + 1]
        if not S.missing[s.name] then
            local arr, why = findAll(scene, s.name)
            if arr == nil then
                if why == "notype" then
                    S.missing[s.name] = true
                    logOnce("missing " .. s.name, "type " .. s.name .. " not found in this game (skipped until the next area load)")
                end
            else
                readTypes = readTypes + 1
                -- window of PER_TYPE_CAP elements starting at winOff; a list longer than the cap is read window by
                -- window over several refreshes (records of the other windows are carried, see below).
                local w0 = S.winOff[s.name] or 0
                local cnt, get, total = arrayAccess(arr, w0 + M.PER_TYPE_CAP)
                cnt = cnt or 0
                total = C.num(total) or cnt
                if w0 >= cnt then w0 = 0 end                         -- the list got shorter: start again
                local wEnd = math.min(cnt, w0 + M.PER_TYPE_CAP)
                if total > M.PER_TYPE_CAP then
                    logOnce("cap " .. s.name, string.format("type %s has %d objects (more than %d): it is read in "
                        .. "windows over several refreshes", s.name, total, M.PER_TYPE_CAP))
                end
                local e0 = S.elemOff[s.name] or w0
                if e0 < w0 or e0 >= wEnd then e0 = w0 end
                S.elemOff[s.name] = nil
                -- "complete" (vanished objects may count) only when the whole list was read in this one refresh
                local complete = total <= M.PER_TYPE_CAP and e0 == 0
                local windowDone = true
                for i = e0, wEnd - 1 do
                    if i > e0 and (i - e0) % 32 == 0 and clock() - t0 > M.REFRESH_HARD_S then
                        -- one big type: go on at this element at the next refresh (no vanish counted for it)
                        S.elemOff[s.name] = i
                        complete, partial, nextRot = false, true, idx
                        windowDone = false
                        break
                    end
                    local comp = get(i)
                    if comp ~= nil then
                        local go = C.pcallm(comp, "get_GameObject")
                        local pos = go and goPos(go, P)
                        local d = pos and ppos and dist(pos, ppos) or nil
                        if pos and (d == nil or d <= M.KEEP_RADIUS) then
                            local tname = C.typeName(comp) or s.name
                            local kind, entry = s.kind, s.entry
                            local okRec = not notItem(P, tname)
                            if okRec and s.src == "base" then
                                local kc = kindCache[tname]
                                if kc == nil then
                                    local kd, _, en = M.classify(C.typeChain(comp), P, L, allowWords)
                                    if kd == nil then
                                        local f = S.found[tname]
                                        kd = f and f.kind or nil
                                        en = f and f.entry or nil          -- seed parent's rules (#8)
                                    end
                                    kc = kd and { kd, en } or false
                                    kindCache[tname] = kc
                                end
                                if kc then kind, entry = kc[1], kc[2] end
                                okRec = kc ~= false
                            end
                            if okRec then
                                local goName = C.pcallm(go, "get_Name")
                                if type(goName) ~= "string" then goName = "" end
                                local key = string.format("%s@%d,%d,%d", goName, floor(pos[1] * 10 + 0.5),
                                    floor(pos[2] * 10 + 0.5), floor(pos[3] * 10 + 0.5))
                                local old = byKey[key]
                                -- one record per GameObject: seed/door > learned > sweep > base
                                local rank = ({ seed = 1, door = 1, learned = 2, sweep = 3, base = 4 })[s.src] or 5
                                if not old or rank < old.rank then
                                    local r = { go = go, comp = comp, tname = tname, kind = kind, entry = entry,
                                                src = s.src, search = s.name, pos = pos, dist = d, rank = rank,
                                                go_name = goName, base = goName .. "|" .. tname }
                                    if old then
                                        for j = 1, #raw do if raw[j] == old then raw[j] = r; break end end
                                    else
                                        raw[#raw + 1] = r
                                    end
                                    byKey[key] = r
                                end
                            end
                        end
                    end
                end
                if windowDone then
                    -- next window (or back to the start); lists within the cap always start at 0
                    S.winOff[s.name] = (total > M.PER_TYPE_CAP and wEnd < total) and wEnd or nil
                end
                if complete then refreshed[s.name] = true end
            end
        end
        if partial then break end
    end
    S.rot = partial and nextRot or 0
    -- carry over records of search types not read completely this time (partial refresh, or the scene call
    -- failed); never longer than CARRY_MAX_S, and not for types that do not exist (missing)
    local inList = {}
    for _, s in ipairs(list) do inList[s.name] = true end
    for _, r in ipairs(S.records) do
        if r.search and not refreshed[r.search] and inList[r.search] and not S.missing[r.search]
            and r.pos and (C.num(r.t) or -huge) >= now - M.CARRY_MAX_S then
            local key = string.format("%s@%d,%d,%d", r.go_name or "", floor(r.pos[1] * 10 + 0.5),
                floor(r.pos[2] * 10 + 0.5), floor(r.pos[3] * 10 + 0.5))
            if not byKey[key] then      -- the same object was read fresh this time (through another type)
                byKey[key] = true
                local d = ppos and dist(r.pos, ppos) or nil
                raw[#raw + 1] = { carried = r, pos = r.pos, dist = d, base = (r.go_name or "") .. "|" .. tostring(r.tname) }
            end
        end
    end
    -- nearest 300
    table.sort(raw, function(a, b)
        local da, db = a.dist or huge, b.dist or huge
        if da ~= db then return da < db end
        return (a.base or "") < (b.base or "")
    end)
    while #raw > M.MAX_RECORDS do raw[#raw] = nil end
    assignIds(raw)
    -- read state (same tick) and build plain records
    S.serial = S.serial + 1
    local serial = S.serial
    local LM = learnMod()
    local out, classes = {}, {}
    for _, r in ipairs(raw) do
        local rec
        if r.carried then
            rec = r.carried
            rec.dist = r.dist
            rec.id = r.id
        else
            rec = { id = r.id, kind = r.kind, name = shortName(r.tname), tname = r.tname, go_name = r.go_name,
                    src = r.src, search = r.search, pos = r.pos, dist = r.dist, obs = serial, t = now,
                    seed = r.entry and r.entry.name or nil }
            if LM and LM.observe then
                local ok, e = pcall(LM.observe, rec, r.go, r.comp, P, r.entry)
                if not ok then pcall(C.err, "scan.observe", e) end
            end
        end
        out[#out + 1] = rec
        classes[rec.tname] = (classes[rec.tname] or 0) + 1
    end
    S.records = out
    S.classes = classes
    S.partial = partial
    S.refreshes = S.refreshes + 1
    S.lastRefreshed = refreshed
    S.lastScanS = clock() - t0
    return out
end

---------------------------------------------------------------------------------------------------------------------
-- sweep (discovery): spread over ticks, fresh array each tick, number offset only
---------------------------------------------------------------------------------------------------------------------
local function startSweep(ppos, now, why)
    S.sweep = { offset = 0, started = now, center = ppos, spent = 0, seen = {}, hits = 0, n = 0, why = why }
    S.lastSweepT = now
    S.lastSweepCenter = ppos
    S.missing = {}                          -- retry missing seed types after an area change
    pcall(C.log, string.format("scan: sweep started (%s)", why))
end

local function finishSweep(now, why, budget)
    local sw = S.sweep
    if not sw then return end
    S.sweep = nil
    S.sweeps = S.sweeps + 1
    S.lastSweepSpent = sw.spent
    S.lastSweepWall = now - sw.started
    S.nextRefresh = -huge                   -- read the new types right away
    -- slow sweep -> wait longer before the next "moved" / periodic sweep (cap: max_interval_s)
    budget = C.num(budget) or 0.05        -- profile discovery.budget_s: sweeps costing over 10x it back off
    if sw.spent > budget * 10 then S.backoff = math.min(S.backoff * 2, 8)
    elseif sw.spent < budget * 2 then S.backoff = 1 end
    pcall(C.log, string.format("scan: sweep %s: %d objects checked, %d types found (%d total), %.3f s of game time"
        .. " (slowest transform fetch %.4f s)", why, sw.n, sw.hits, #S.foundList, sw.spent, sw.fetchMax or 0))
end

local function sweepChunk(P, L, scene, ppos, now)
    local sw = S.sweep
    local budget = type(P) == "table" and type(P.discovery) == "table" and P.discovery.budget_s or nil
    if now - sw.started > M.SWEEP_MAX_S then finishSweep(now, "stopped (too long)"); return end
    if sw.nextWork and now < sw.nextWork and now >= sw.started then return end   -- paced after a slow fetch
    -- ONE fetch per sweep tick; the array is used only in this tick (never kept)
    local tf = clock()
    local arr = findAll(scene, "via.Transform")
    if arr == nil then finishSweep(now, "stopped (no transforms)"); return end
    local cnt, get = arrayAccess(arr, 1000000)
    if cnt == nil then finishSweep(now, "stopped (array)"); return end
    local t0 = clock()                         -- the walk budget starts AFTER the fetch
    local fetchS = t0 - tf
    if fetchS > (sw.fetchMax or 0) then sw.fetchMax = fetchS end
    -- the walk gets at least as long as the fetch took (so a slow fetch still buys real progress), capped
    local walkS = math.max(M.SWEEP_CHUNK_S, math.min(fetchS, M.SWEEP_WALK_MAX_S))
    local chunkMax = math.floor(M.SWEEP_CHUNK_MAX * walkS / M.SWEEP_CHUNK_S)
    local radius = (C.num(type(P) == "table" and P.range) or M.RANGE) * M.SWEEP_RADIUS_MULT
    local center = ppos or sw.center
    local allowWords = wordsAllowed(P)
    local i, done = sw.offset, 0
    while i < cnt do
        if done >= chunkMax or (done > 0 and clock() - t0 > walkS) then break end
        local tr = get(i)
        i, done = i + 1, done + 1
        if tr ~= nil then
            local pos = C.toM(C.pcallm(tr, "get_Position"), P)        -- same scale as the player (#3)
            if pos and center and dist(pos, center) <= radius then
                sw.n = sw.n + 1
                local go = C.pcallm(tr, "get_GameObject")
                local comps = go and C.elements(C.pcallm(go, "get_Components"), M.COMP_CAP) or {}
                for _, comp in ipairs(comps) do
                    local tname = C.typeName(comp)
                    if tname and sw.seen[tname] == nil then
                        local kind, reason, entry = nil, nil, nil
                        if not S.found[tname] then
                            kind, reason, entry = M.classify(C.typeChain(comp), P, L, allowWords)
                        end
                        sw.seen[tname] = kind or false
                        -- seeds, profile doors and learned types are searched anyway; add only new guesses
                        if kind and reason and (reason:find("^word") or reason:find("parent")) then
                            if #S.foundList < M.FOUND_CAP then
                                -- entry: the seed/door entry of the matching parent, so its require/gone/unlocked
                                -- rules stay in force for this type
                                S.found[tname] = { kind = kind, reason = reason, entry = entry }
                                S.foundList[#S.foundList + 1] = tname
                                sw.hits = sw.hits + 1
                                pcall(C.log, string.format("scan: new %s type %s (%s)", kind, tname, reason))
                            elseif not S.foundFull then
                                S.foundFull = true
                                pcall(C.log, "scan: found-type list is full (" .. M.FOUND_CAP .. "); more are ignored")
                            end
                        end
                    end
                end
            end
        end
    end
    sw.offset = i
    local cost = clock() - tf                  -- fetch + walk: the real cost of this tick
    sw.spent = sw.spent + cost
    if i >= cnt then finishSweep(now, "done", budget); return end
    -- pacing: keep the sweep's average near SWEEP_CHUNK_S per 0.1 s tick when one tick cost more than that
    if cost > M.SWEEP_CHUNK_S then
        sw.nextWork = now + math.min(M.SWEEP_WAIT_MAX_S, (cost / M.SWEEP_CHUNK_S - 1) * 0.1)
    else
        sw.nextWork = nil
    end
end

local function discovery(P, L, scene, ppos, now, work)
    local d = type(P) == "table" and type(P.discovery) == "table" and P.discovery or {}
    if d.mode == "off" then
        S.sweep, S.areaSweep = nil, false
        return
    end
    if S.sweep == nil and ppos then
        local minI = (C.num(d.min_interval_s) or 120) * S.backoff
        local maxI = C.num(d.max_interval_s) or 600
        if minI > maxI then minI = maxI end
        local moveM = C.num(d.move_m) or 200
        local why = nil
        local since = S.lastSweepT and (now - S.lastSweepT) or huge
        if S.lastSweepT == nil then why = "first"
        elseif S.areaSweep and since >= M.AREA_MIN_S then why = "area load"
        elseif S.lastSweepCenter and dist(ppos, S.lastSweepCenter) > moveM and since >= minI then why = "moved"
        elseif since >= maxI then why = "periodic" end
        if why then
            S.areaSweep = false
            startSweep(ppos, now, why)
        end
    end
    if S.sweep and work then sweepChunk(P, L, scene, ppos, now) end
end

---------------------------------------------------------------------------------------------------------------------
-- main entry
---------------------------------------------------------------------------------------------------------------------
function M.scan(P, world, now, L)
    world = type(world) == "table" and world or {}
    now = C.num(now) or clock()
    if L == nil then
        local LM = learnMod()
        L = LM and LM.learned and LM.learned() or nil
    end
    local ppos = posOf(world.player)
    local info = { serial = S.serial, changed = false }
    -- area change: ctx.area changed, or the player jumped more than move_m since the last tick.
    -- S.areaChanged: the next refresh is told (learn must not count the objects that left as "vanished");
    -- S.areaSweep: a sweep is still owed for the new area (kept until a sweep starts).
    local moveM = C.num(type(P) == "table" and type(P.discovery) == "table" and P.discovery.move_m) or 200
    local area = world.area
    local changedNow = false
    if area ~= nil and S.lastArea ~= nil and area ~= S.lastArea then changedNow = true end
    if area ~= nil then S.lastArea = area end
    if ppos and S.lastPos and dist(ppos, S.lastPos) > moveM then changedNow = true end
    if ppos then S.lastPos = ppos end
    if changedNow then
        S.areaChanged, S.areaSweep, S.nextRefresh = true, true, -huge
        S.missing = {}
    end

    if ppos == nil then
        return S.records, info
    end
    local scene = getScene(world)
    if scene == nil then
        logOnce("noscene", "no scene yet (waiting)")
        return S.records, info
    end

    if now >= S.nextRefresh then
        refresh(P, L, scene, ppos, now)
        local nearest = huge
        for _, r in ipairs(S.records) do
            if r.dist and r.dist < nearest then nearest = r.dist end
        end
        local LM = learnMod()
        local pend = LM and LM.hasPending and LM.hasPending() or false
        S.nextRefresh = now + ((nearest <= M.NEAR_REFRESH_M or pend) and M.REFRESH_NEAR_S or M.REFRESH_S)
        info.changed = true
        info.area_changed = S.areaChanged
        S.areaChanged = false
        info.refreshed = S.lastRefreshed
        info.partial = S.partial
        info.scan_s = S.lastScanS
        -- no sweep work in the same tick as a refresh (keeps one tick short); a new sweep may still be started
        if S.sweep == nil then discovery(P, L, scene, ppos, now, false) end
    else
        discovery(P, L, scene, ppos, now, true)
    end
    info.serial = S.serial
    info.sweeping = S.sweep ~= nil
    info.sweeps = S.sweeps
    info.found = #S.foundList
    info.classes = S.classes
    return S.records, info
end

function M.tick(ctx)
    if type(ctx) ~= "table" then return end
    local recs, info = M.scan(ctx.profile, { player = ctx.player, area = ctx.area }, ctx.now)
    ctx.records = recs
    ctx.scan_info = info
    ctx.classes = S.classes or {}
    return recs
end

return M
