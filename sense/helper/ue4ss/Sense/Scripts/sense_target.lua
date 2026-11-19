-- sense_target.lua - the "where should I go" target for the quest path (read-only, never changes the game).
--
--   M.read(pc, pawn, P, ctx)                  -> target | nil   what the game itself tracks (profile target type)
--   M.unlockedTarget(doorEvents, playerPos, now) -> target | nil the most recent unlocked door not reached yet
--   M.choose(tracked, unlocked)               -> target | nil   tracked first, then unlocked, else nil
--   M.elsewhere()                             -> {label=, map=} | nil  set by the LAST read(): the tracked main quest
--                                                is on another map (read() then gave nil); main.lua sends it as the
--                                                top-level "target_elsewhere" key and sends no target meanwhile
--   M.reset()                                  forget all session memory (tests / map change)
--
-- Quest target rules:
--   MAIN-ONLY main quests only: a tracked quest gives a target only when it is the main quest
--      (PBZNarrativeComponent.CurrentMainQuest valid and the same object as CurrentTrackedQuest, by GetFullName), or
--      else when its short name starts with "Main" (MainCh2-XinZhiZongJi_C_...). Can't tell -> not main -> no quest
--      target and no "elsewhere" note. A tracked map lock and the unlocked-door fallback are not affected.
--   OTHER-AREA other area: GetTrackedPointResolved's OutMapID vs MapSubsystem:GetCurrentMapID() (MapSubsystem from
--      FindAllOf, Default__ skipped, cached + IsValid each use). Different -> no target + M.elsewhere(). The current
--      map id can't be read -> the target is sent as before + one log line "target: map check unavailable ...".
--   UNLOCK-BATCH unlock filter for the unlocked-door fallback: unlock events are grouped into batches by their time (events at
--      most 1.25 s apart join one batch, because main.lua reads each door on its own 1 s timer, so one mass flip
--      reaches us over several ticks); a batch is decided 1.25 s after its newest event (held until then, so a real
--      unlock becomes a target ~1.25 s later). 3 or more events in a batch are all ignored (story / loading flips
--      many locks at once; one log line per batch "target: ignored N unlocks at
--      once"); a lock on another map is skipped: MapSubsystem:GetMapIDByLocation(cm vector) vs the current map id,
--      else the level name in the lock id (GuidString "PBZ_Pangzhen_WP-...") vs the player's level; neither -> kept.
--
-- target = { pos = {x,y,z} (metres, UE: cm/100, Z up), has_z = true, kind = "quest"|"lock"|"door",
--            id = "text", label = "short text or empty", src = "tracked"|"unlocked" }
--
-- ctx (all optional): { log = function(text), findAll = function(className) -> list|nil (raises on a real error),
--                       now = function() -> seconds, array = function(t) -> t (JSON array marker) }
--
-- Rules (crash history, see main.lua): every game touch is inside pcall; no generic Array/Map/Struct reads (only the
-- named props below); no object-wide walks, no hooks; the narrative component is taken through the pawn, never by a
-- "first object of this class" lookup (the Default__ templates come first). The caller runs read() inside the game
-- thread with its in-flight guard.

local M = {}

-- ---------- settings ----------
local REACH_H_M      = 3.0      -- reached = within 3 m horizontal ...
local REACH_V_M      = 2.5      -- ... and 2.5 m vertical
local FORGET_S       = 3600.0   -- an unlocked door is forgotten after 60 min
local POINT_SCAN_S   = 5.0      -- quest point actor search at most this often while nothing matched
local MAX_POINTS     = 256      -- most quest point actors looked at in one search
local MAX_FAILS      = 3        -- a way that errors / gives no usable shape this many times in a row is switched off
local MAX_LABEL      = 40
local MAP_CACHE_S    = 0.5      -- the current map id is read at most this often
local MAPSUB_SCAN_S  = 5.0      -- MapSubsystem search at most this often while none was found
local MAX_MAPSUBS    = 16       -- most MapSubsystem objects looked at in one search
local MASS_UNLOCK_N  = 3        -- this many unlock events in one batch = a mass state flip: all ignored (UNLOCK-BATCH)
local BATCH_GAP_S    = 1.25     -- UNLOCK-BATCH: unlock events at most this far apart (by ev.t) belong to one batch. main.lua reads
                                -- each door on its own 1.0 s timer (DOOR_S), so one game-frame flip of many locks
                                -- reaches us spread over up to DOOR_S + one tick; 1.25 s covers that with slack.
                                -- A batch is decided once no event came for this long, so a new unlock becomes a
                                -- target about this much later than it is seen.

-- ---------- session memory ----------
local S = {}
local function freshState()
    S = {
        logged = {},          -- message key -> true (each log line once)
        fails = {},           -- way -> consecutive failures
        off = {},             -- way -> true when switched off for this session
        pointKey = nil,       -- the "quest_branch" / task point key the cached point actor belongs to
        pointObj = nil,       -- cached matching quest point actor
        pointId = nil,
        pointScanAt = -math.huge,
        pointScanKey = nil,
        reached = {},         -- door event key -> time it was reached
        reachedN = 0,
        questKey = nil,       -- lock guid | quest | task | branch the fail counters belong to
        mapSub = nil,         -- cached MapSubsystem object (IsValid checked on every use)
        mapSubScanAt = -math.huge,
        curMap = nil,         -- current map id text (GetCurrentMapID) or nil = unknown
        curMapAt = nil,       -- clock of the last current map read
        playerLevel = nil,    -- the pawn's level name ("PBZ_Pangzhen_WP"), from its full name in read()
        elsewhere = nil,      -- {label, map} when the last read() found the main quest on another map (OTHER-AREA)
        seenEv = {},          -- door event key -> its t (every event is counted as "new" once, UNLOCK-BATCH)
        ignoredEv = {},       -- door event key -> true: part of a mass unlock batch (UNLOCK-BATCH)
        batch = {},           -- UNLOCK-BATCH open batch: list of {ev=, key=} not decided yet (held: not a candidate)
        batchKey = {},        -- door event key -> true while it is in the open batch
        batchLast = nil,      -- the largest ev.t in the open batch
        evMap = {},           -- door event key -> {map = map id text|nil, level = level name|nil} (UNLOCK-BATCH)
    }
end
freshState()
function M.reset() freshState() end
M._state = function() return S end   -- for tests

-- ---------- small helpers (all pcall) ----------
local function clock(ctx)
    if ctx and type(ctx.now) == "function" then
        local ok, v = pcall(ctx.now)
        if ok and type(v) == "number" then return v end
    end
    return os.clock()
end

local function logOnce(ctx, key, text)
    if S.logged[key] then return end
    S.logged[key] = true
    local line = "target: " .. text
    if ctx and type(ctx.log) == "function" then
        if pcall(ctx.log, line) then return end
    end
    pcall(print, "[Sense] " .. line .. "\n")
end

-- a log line every time (used once per mass unlock batch)
local function logLine(ctx, text)
    local line = "target: " .. text
    if ctx and type(ctx.log) == "function" then
        if pcall(ctx.log, line) then return end
    end
    pcall(print, "[Sense] " .. line .. "\n")
end

local function valid(obj)
    if obj == nil then return false end
    local ok, v = pcall(function() return obj:IsValid() end)
    return ok and v == true
end

local function fullName(obj)
    local ok, s = pcall(function() return obj:GetFullName() end)
    if ok and type(s) == "string" then return s end
    return nil
end

local function isTemplate(obj)
    local n = fullName(obj)
    return n ~= nil and n:find("Default__", 1, true) ~= nil
end

local function goodNum(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

-- a Name / String property value (userdata with ToString, or a plain Lua string) -> text; "" when empty or "None"
local function toText(v)
    if v == nil then return "" end
    if type(v) == "string" then
        if v == "None" then return "" end
        return v
    end
    local ok, s = pcall(function() return v:ToString() end)
    if ok and type(s) == "string" and s ~= "None" then return s end
    return ""
end

local function prop(obj, name)
    local ok, v = pcall(function() return obj[name] end)
    if ok then return v end
    return nil
end

-- calls obj:fname(...) in pcall; returns ok, then every result packed in a table with .n
local function callAll(obj, fname, ...)
    local args = { ... }
    local nargs = select("#", ...)
    local res = { pcall(function()
        local f = obj[fname]
        if f == nil then error("no function " .. tostring(fname)) end
        return f(obj, table.unpack(args, 1, nargs))
    end) }
    local ok = table.remove(res, 1)
    res.n = #res
    return ok == true, res
end

-- one vector-like value -> {x,y,z} in cm, or nil. Accepts X/Y/Z, x/y/z, [1..3]; looks one level deeper into plain
-- Lua tables (the out-param table UE4SS fills may hold the struct under some key). Userdata is only read by name.
local function vecCm(v, depth)
    if v == nil then return nil end
    local tv = type(v)
    if tv ~= "table" and tv ~= "userdata" then return nil end
    local ok, x, y, z = pcall(function() return v.X, v.Y, v.Z end)
    if ok and goodNum(x) and goodNum(y) and goodNum(z) then return { x, y, z } end
    ok, x, y, z = pcall(function() return v.x, v.y, v.z end)
    if ok and goodNum(x) and goodNum(y) and goodNum(z) then return { x, y, z } end
    if tv == "table" then
        x, y, z = rawget(v, 1), rawget(v, 2), rawget(v, 3)
        if goodNum(x) and goodNum(y) and goodNum(z) then return { x, y, z } end
        if (depth or 0) < 2 then
            local found = nil
            pcall(function()
                for _, inner in pairs(v) do
                    local tt = type(inner)
                    if tt == "table" or tt == "userdata" then
                        found = vecCm(inner, (depth or 0) + 1)
                        if found then return end
                    end
                end
            end)
            if found then return found end
        end
    end
    return nil
end

local function cmToM(c) return { c[1] / 100, c[2] / 100, c[3] / 100 } end

local function isZero(c) return c[1] == 0 and c[2] == 0 and c[3] == 0 end

local function short(s)
    s = s or ""
    if #s > MAX_LABEL then s = s:sub(1, MAX_LABEL) end
    return s
end

local function makeTarget(ctx, posM, kind, id, label, src)
    local p = { posM[1], posM[2], posM[3] }
    if ctx and type(ctx.array) == "function" then
        local ok, r = pcall(ctx.array, p)
        if ok and type(r) == "table" then p = r end
    end
    return { pos = p, has_z = true, kind = kind, id = tostring(id or ""), label = short(label), src = src }
end

local function fail(ctx, way, why)
    S.fails[way] = (S.fails[way] or 0) + 1
    if S.fails[way] >= MAX_FAILS and not S.off[way] then
        S.off[way] = true
        logOnce(ctx, "off:" .. way, "way '" .. way .. "' switched off for this session after " .. MAX_FAILS ..
            " failures in a row (last: " .. tostring(why) .. ")")
    end
end

local function worked(ctx, way, how)
    S.fails[way] = 0
    logOnce(ctx, "ok:" .. way, "tracked target read via " .. how)
end

-- "nothing there yet" (no lock object, no point actors loaded) is not a failure; a way is only switched off
-- after real errors or wrong shapes. When the tracked lock / quest / task / branch changes, every quest way gets a
-- fresh start (fail counters to 0, switched-off ways back on).
local QUEST_WAYS = { "lock", "quest_call", "quest_point" }
local function newQuestKey(ctx, key)
    if S.questKey == key then return end
    local had = S.questKey ~= nil
    S.questKey = key
    for _, way in ipairs(QUEST_WAYS) do
        if had and S.off[way] then
            logOnce(ctx, "on:" .. way .. ":" .. key, "way '" .. way .. "' switched on again (tracked quest/lock changed)")
        end
        S.fails[way] = 0
        S.off[way] = nil
        S.logged["off:" .. way] = nil
    end
end

-- describes what a call gave back, for the one-time log when no shape fitted (helps fix the out-param handling)
local function describe(res, outs)
    local parts = {}
    for i = 1, res.n or #res do parts[#parts + 1] = "ret" .. i .. "=" .. type(res[i]) end
    for _, name in ipairs({ "outMap", "outLoc" }) do
        local keys = {}
        pcall(function()
            for k, v in pairs(outs[name]) do
                keys[#keys + 1] = tostring(k) .. ":" .. type(v)
                if #keys >= 6 then break end
            end
        end)
        parts[#parts + 1] = name .. "{" .. table.concat(keys, ",") .. "}"
    end
    return table.concat(parts, " ")
end

-- the map id out-param (and the returned values) as short text, for one log line. Only plain values and Name/String
-- ToString are read; at most 6 keys of a plain Lua table.
local function describeMap(outMap, res)
    local function one(v)
        local tv = type(v)
        if tv == "string" or tv == "number" or tv == "boolean" then return tv .. "=" .. tostring(v) end
        if tv == "userdata" then
            local s = toText(v)
            return "userdata" .. (s ~= "" and ("=" .. s:sub(1, 60)) or "")
        end
        return tv
    end
    local parts = {}
    pcall(function()
        if type(outMap) == "table" then
            local keys = {}
            for k, v in pairs(outMap) do
                keys[#keys + 1] = tostring(k) .. ":" .. one(v)
                if #keys >= 6 then break end
            end
            parts[#parts + 1] = "outMap{" .. table.concat(keys, ",") .. "}"
        else
            parts[#parts + 1] = "outMap " .. one(outMap)
        end
        for i = 1, (res and res.n) or 0 do
            if i > 4 then break end
            parts[#parts + 1] = "ret" .. i .. " " .. one(res[i])
        end
    end)
    return table.concat(parts, " ")
end

-- the quest's map id as text ("PangZhen") from GetTrackedPointResolved's results, or "" when none can be read.
-- Real UE4SS (log 2026-11-05): outMap.OutMapID = Name. Also accepted: outMap.MapID / outMap[1], a returned struct
-- with .OutMapID, or a returned Name. Only Name/String ToString (in pcall) and plain values are read.
local function mapIdFrom(outMap, res)
    local s = ""
    pcall(function()
        if type(outMap) == "table" then
            s = toText(rawget(outMap, "OutMapID"))
            if s == "" then s = toText(rawget(outMap, "MapID")) end
            if s == "" then s = toText(rawget(outMap, 1)) end
        end
        if s ~= "" then return end
        for i = 1, (res and res.n) or 0 do
            if i > 4 then break end
            local r = res[i]
            if r ~= nil and r ~= outMap and type(r) ~= "boolean" and type(r) ~= "number" then
                local inner = type(r) == "table" and rawget(r, "OutMapID") or nil
                if inner ~= nil then s = toText(inner) else s = toText(r) end
                if s ~= "" then return end
            end
        end
    end)
    if type(s) ~= "string" then return "" end
    return s
end

-- ---------- the three ways ----------

-- way 1: a tracked map lock (door / gate / blocker icon tracked on the map)
local function readLock(ctx, comp, guid)
    if S.off.lock then return nil end
    if guid == nil then guid = toText(prop(comp, "TrackedMapLockGuid")) end
    if guid == "" then return nil end
    local ok, res = callAll(comp, "GetTrackedMapLock")
    if not ok then fail(ctx, "lock", tostring(res[1])); return nil end   -- a real call error
    local lock = res[1]
    -- no lock object (loading, or the lock is on another map): nothing yet, not a failure
    if not valid(lock) then return nil end
    local c = vecCm(prop(lock, "ActorLocation"), 1)
    if not c or isZero(c) then fail(ctx, "lock", "no ActorLocation"); return nil end
    worked(ctx, "lock", "GetTrackedMapLock().ActorLocation")
    return makeTarget(ctx, cmToM(c), "lock", guid, "", "tracked")
end

-- way 2: GetTrackedPointResolved(out MapID, out Location) -> bool
local function readQuestCall(ctx, comp)
    if S.off.quest_call then return nil end
    local outMap, outLoc = {}, {}
    local ok, res = callAll(comp, "GetTrackedPointResolved", outMap, outLoc)
    if not ok then fail(ctx, "quest_call", res[1]); return nil end
    -- an explicit false means "nothing resolved" (not a failure of the way)
    for i = 1, res.n do
        if res[i] == false then S.fails.quest_call = 0; return nil end
    end
    -- the location: the filled out table first, then any returned struct / table (but never the MapID table)
    local c = vecCm(outLoc, 0)
    if not c then
        for i = 1, res.n do
            local r = res[i]
            if r ~= outMap and (type(r) == "table" or type(r) == "userdata") then
                c = vecCm(r, 0)
                if c then break end
            end
        end
    end
    if not c then
        logOnce(ctx, "shape:quest_call", "GetTrackedPointResolved gave no usable vector (" ..
            describe(res, { outMap = outMap, outLoc = outLoc }) .. ")")
        fail(ctx, "quest_call", "no vector shape")
        return nil
    end
    if isZero(c) then return nil end   -- an unresolved point; not counted as a failure
    worked(ctx, "quest_call", "GetTrackedPointResolved out-param")
    -- once: what the map id out-param holds (unverified whether the game also returns points on other maps)
    if not S.logged["outmap"] then
        logOnce(ctx, "outmap", "GetTrackedPointResolved map id: " .. describeMap(outMap, res))
    end
    return makeTarget(ctx, cmToM(c), "quest", nil, nil, "tracked"), c, mapIdFrom(outMap, res)
end

-- the short quest name ("MainCh2-XinZhiZongJi") from the quest object: class name or object name, without "_C"
-- and a trailing instance number
local function questNames(q)
    local names, seen = {}, {}
    local function add(s)
        if type(s) ~= "string" or s == "" or s == "None" then return end
        s = s:match("([^%.:/]+)$") or s
        s = s:gsub("_C_%d+$", ""):gsub("_C$", ""):gsub("_%d%d%d%d%d+$", "")
        if s ~= "" and not seen[s] then seen[s] = true; names[#names + 1] = s end
    end
    -- the object's own name first (a quest that is a plain "PBZQuest" instance otherwise got the
    -- label "PBZQuest"; a per-quest blueprint "X_C_0" still gives "X" here), then the class name
    pcall(function() add(q:GetFName():ToString()) end)
    pcall(function() add(q:GetClass():GetFName():ToString()) end)
    return names
end

-- list | nil (nothing found: nil or an empty list), plus an error text only for a real error or a wrong shape
local function findAll(ctx, cls)
    local ok, list
    if ctx and type(ctx.findAll) == "function" then
        ok, list = pcall(ctx.findAll, cls)
    else
        ok, list = pcall(function() return FindAllOf(cls) end)
    end
    if not ok then return nil, "FindAllOf error: " .. tostring(list) end
    if list == nil then return nil end
    if type(list) ~= "table" then return nil, "FindAllOf gave a " .. type(list) end
    if next(list) == nil then return nil end
    return list
end

-- ---------- maps (OTHER-AREA / UNLOCK-BATCH) ----------
local function lower(s) return type(s) == "string" and s:lower() or "" end

local function objName(obj)
    local ok, s = pcall(function() return obj:GetFName():ToString() end)
    if ok and type(s) == "string" then return s end
    return nil
end

-- the level name ("PBZ_Pangzhen_WP") from a full name "Class /Game/Maps/B08_09_10/PBZ_Pangzhen_WP.PBZ_Pangzhen_WP:
-- PersistentLevel.X" (or the same without the class part); nil when it has no such part
local function levelOfFull(full)
    if type(full) ~= "string" then return nil end
    local pkg = full:match("^%S+%s+([^:%s]+):") or full:match("^([^:%s]+):")
    if not pkg or not pkg:find("/", 1, true) then return nil end
    local lv = pkg:match("([^%./]+)$")
    if lv == nil or lv == "" then return nil end
    return lv
end

-- the level name of a door event id: a full name (as above) or a PBZ GuidString "PBZ_Pangzhen_WP-BP_ThornyVine_C_..."
-- (a letter first and an "_" in the part before the first "-", so a plain hex GUID gives nil)
local function levelOfId(id)
    if type(id) ~= "string" then return nil end
    local lv = levelOfFull(id)
    if lv then return lv end
    return id:match("^(%a[%w_]*_[%w_]*)%-")
end

-- the MapSubsystem object (not a Default__ template), cached; nil when none (searched at most every MAPSUB_SCAN_S)
local function mapSubsystem(ctx, now)
    if S.mapSub ~= nil then
        if valid(S.mapSub) then return S.mapSub end
        S.mapSub = nil
    end
    if now - S.mapSubScanAt >= 0 and now - S.mapSubScanAt < MAPSUB_SCAN_S then return nil end
    S.mapSubScanAt = now
    local list, err = findAll(ctx, "MapSubsystem")
    if err then logOnce(ctx, "mapsub:err", "MapSubsystem search failed (" .. tostring(err) .. ")"); return nil end
    if not list then return nil end
    local n = 0
    for _, o in ipairs(list) do
        n = n + 1
        if n > MAX_MAPSUBS then break end
        if valid(o) then
            local nm, full = objName(o), fullName(o)
            local template = (nm ~= nil and nm:sub(1, 9) == "Default__") or (full ~= nil and full:find("Default__", 1, true) ~= nil)
            if not template and (nm ~= nil or full ~= nil) then
                S.mapSub = o
                logOnce(ctx, "mapsub:ok", "MapSubsystem found (map checks on)")
                return o
            end
        end
    end
    return nil
end

-- the current map id text ("PangZhen") or nil when it can't be read (no MapSubsystem, call error, empty/"None")
local function currentMapId(ctx, now)
    if S.curMapAt ~= nil and now - S.curMapAt >= 0 and now - S.curMapAt < MAP_CACHE_S then return S.curMap end
    S.curMapAt = now
    S.curMap = nil
    if S.off.mapcheck then return nil end
    local sub = mapSubsystem(ctx, now)
    if sub == nil then return nil end
    local ok, res = callAll(sub, "GetCurrentMapID")
    if not ok then fail(ctx, "mapcheck", tostring(res[1])); return nil end
    local s = toText(res[1])
    if s == "" then return nil end
    S.fails.mapcheck = 0
    S.curMap = s
    return s
end

local function mapCheckUnavailable(ctx)
    logOnce(ctx, "mapcheck:na", "map check unavailable (no MapSubsystem or GetCurrentMapID gave nothing): " ..
        "targets are sent without it")
end

-- which map a door event is on: {map = GetMapIDByLocation text | nil, level = level name from its id | nil}
local function eventMap(ctx, ev, now)
    local info = { level = levelOfId(ev.id) }
    local sub = mapSubsystem(ctx, now)
    if sub ~= nil then
        local loc = { X = ev.pos[1] * 100, Y = ev.pos[2] * 100, Z = ev.pos[3] * 100 }
        local ok, res = callAll(sub, "GetMapIDByLocation", loc)
        if ok then
            local s = toText(res[1])
            if s ~= "" then info.map = s end
        else
            logOnce(ctx, "maploc:err", "GetMapIDByLocation failed (" .. tostring(res[1]) .. "): the level name in " ..
                "the lock id is used instead")
        end
    end
    return info
end

-- false = this door event is on another map than the player (UNLOCK-BATCH); true = same map or can't be told
local function eventOnThisMap(ctx, key, now)
    local info = S.evMap[key]
    if type(info) ~= "table" then return true end
    if info.map then
        local cur = currentMapId(ctx, now)
        if cur then
            if lower(cur) == lower(info.map) then return true end
            logOnce(ctx, "othermap:" .. key, "unlocked " .. key .. " is on map " .. info.map .. ", the player on " ..
                cur .. ": ignored")
            return false
        end
    end
    if info.level and S.playerLevel then
        if lower(info.level) == lower(S.playerLevel) then return true end
        logOnce(ctx, "otherlevel:" .. key, "unlocked " .. key .. " is in level " .. info.level .. ", the player in " ..
            S.playerLevel .. ": ignored")
        return false
    end
    return true
end

-- MAIN-ONLY: is the tracked quest the main quest? CurrentMainQuest same object (by full name), else short name "Main..."
local function isMainQuest(comp, q, shortName)
    local mq = prop(comp, "CurrentMainQuest")
    if valid(mq) then
        local a, b = fullName(mq), fullName(q)
        if a ~= nil and b ~= nil and a == b then return true end
    end
    return type(shortName) == "string" and shortName:sub(1, 4) == "Main"
end

local function pointId(a)
    local pc = prop(a, "MapTargetPointComponent")
    if not valid(pc) then return "" end
    return toText(prop(pc, "TargetPointActorID"))
end

local function actorCm(a)
    local ok, x, y, z = pcall(function() local l = a:K2_GetActorLocation(); return l.X, l.Y, l.Z end)
    if ok and goodNum(x) and goodNum(y) and goodNum(z) then return { x, y, z } end
    return nil
end

-- way 3: match quest name + "_" + TrackedBranchID (or TrackedTaskPointID) against the quest point actors
local function readQuestPoint(ctx, comp, q, now)
    if S.off.quest_point then return nil end
    local wanted, order = {}, {}
    local function want(s) if s ~= "" and not wanted[s] then wanted[s] = #order + 1; order[#order + 1] = s end end
    local task = toText(prop(comp, "TrackedTaskPointID"))
    local branch = toText(prop(comp, "TrackedBranchID"))
    local qnames = questNames(q)
    want(task)
    if branch ~= "" then for _, n in ipairs(qnames) do want(n .. "_" .. branch) end end
    if #order == 0 then return nil end
    local key = table.concat(order, "|")
    local label = qnames[1] or ""

    -- cached actor for the same key: only its location is read again
    if S.pointKey == key and S.pointObj ~= nil then
        if valid(S.pointObj) and pointId(S.pointObj) == S.pointId then
            local c = actorCm(S.pointObj)
            if c and not isZero(c) then return makeTarget(ctx, cmToM(c), "quest", S.pointId, label, "tracked") end
        end
        S.pointObj, S.pointId = nil, nil
    end
    if S.pointScanKey == key and now - S.pointScanAt < POINT_SCAN_S then return nil end
    S.pointScanKey, S.pointScanAt = key, now

    local list, err = findAll(ctx, "MapTargetPointActor")
    if err then fail(ctx, "quest_point", err); return nil end   -- a real error or a wrong shape
    if not list then return nil end   -- nil / empty: no point actor loaded yet, not a failure
    local best, bestRank, bestId = nil, nil, nil
    local n = 0
    for _, a in ipairs(list) do
        n = n + 1
        if n > MAX_POINTS then break end
        if valid(a) and not isTemplate(a) then
            local id = pointId(a)
            local rank = wanted[id]
            if rank and (bestRank == nil or rank < bestRank) then best, bestRank, bestId = a, rank, id end
        end
    end
    if not best then return nil end   -- nothing matched yet (not counted: the point may load later)
    local c = actorCm(best)
    if not c or isZero(c) then fail(ctx, "quest_point", "no actor location"); return nil end
    S.pointKey, S.pointObj, S.pointId = key, best, bestId
    worked(ctx, "quest_point", "MapTargetPointActor id match (" .. bestId .. ")")
    return makeTarget(ctx, cmToM(c), "quest", bestId, label, "tracked")
end

local function readNarrative(pc, pawn, P, ctx)
    if not valid(pawn) then
        local p = prop(pc, "Pawn")
        if valid(p) then pawn = p else return nil end
    end
    if isTemplate(pawn) then return nil end
    local lv = levelOfFull(fullName(pawn))
    if lv then S.playerLevel = lv end
    local spec = (type(P) == "table" and type(P.target) == "table") and P.target or {}
    local compName = type(spec.component) == "string" and spec.component or "PBZNarrativeComponent"
    local comp = prop(pawn, compName)
    if not valid(comp) then
        local ok, res = callAll(pawn, "GetNarrativeComp")
        comp = ok and res[1] or nil
    end
    if not valid(comp) or isTemplate(comp) then
        logOnce(ctx, "nocomp", "the pawn has no " .. compName .. " (no tracked target)")
        return nil
    end
    local now = clock(ctx)

    -- what is tracked right now; a change gives every quest way a fresh start
    local guid = toText(prop(comp, "TrackedMapLockGuid"))
    local q = prop(comp, "CurrentTrackedQuest")
    local qok = valid(q)
    local idTask, branch, qname = "", "", ""
    if qok then
        idTask = toText(prop(comp, "TrackedTaskPointID"))
        branch = toText(prop(comp, "TrackedBranchID"))
        qname = fullName(q) or "?"
    end
    newQuestKey(ctx, guid .. "|" .. qname .. "|" .. idTask .. "|" .. branch)

    local t = readLock(ctx, comp, guid)
    if t then return t end

    if not qok then
        S.pointObj, S.pointId, S.pointKey = nil, nil, nil
        return nil
    end
    local label = questNames(q)[1] or ""
    -- MAIN-ONLY: main quests only (a side quest gives no trail and no "elsewhere" note)
    if not isMainQuest(comp, q, label) then
        logOnce(ctx, "side:" .. label, "tracked quest '" .. label .. "' is not a main quest: no quest target " ..
            "(main quests only)")
        return nil
    end
    -- TrackedBranchID "None"/empty = between quest steps: no tracked quest target (the lock above and the unlocked
    -- door fallback still apply).
    if branch == "" then
        logOnce(ctx, "between", "the tracked quest has no branch (between steps): no quest target until the next step")
        return nil
    end
    local qt, _qcm, qmap = readQuestCall(ctx, comp)
    t = qt
    if t then
        t.label = short(label)
        if idTask ~= "" then t.id = idTask
        elseif label ~= "" then t.id = label .. (branch ~= "" and ("_" .. branch) or "")
        else t.id = "quest" end
        -- OTHER-AREA: the quest point is on another map than the player -> no target, "elsewhere" note instead
        if type(qmap) == "string" and qmap ~= "" then
            local cur = currentMapId(ctx, now)
            if cur == nil then
                mapCheckUnavailable(ctx)
            elseif lower(cur) ~= lower(qmap) then
                S.elsewhere = { label = short(label), map = qmap }
                logOnce(ctx, "elsewhere:" .. label .. "|" .. qmap .. "|" .. cur, "main quest '" .. label .. "' is on map " ..
                    qmap .. ", the player is on " .. cur .. ": no target (target_elsewhere sent)")
                return nil
            end
        else
            logOnce(ctx, "mapcheck:noid", "GetTrackedPointResolved gave no map id: no map check for the quest")
        end
        return t
    end
    t = readQuestPoint(ctx, comp, q, now)
    if t then return t end
    logOnce(ctx, "noquestpos", "a quest is tracked but no way gave its position yet")
    return nil
end

-- what the game itself tracks, by profile target type. Never raises.
function M.read(pc, pawn, P, ctx)
    S.elsewhere = nil
    local spec = type(P) == "table" and P.target or nil
    if type(spec) ~= "table" then return nil end
    if spec.type == "pbz_narrative" then
        local ok, r = pcall(readNarrative, pc, pawn, P, ctx)
        if ok then return r end
        logOnce(ctx, "err:" .. tostring(r):sub(1, 60), "read failed: " .. tostring(r))
        return nil
    end
    if spec.type ~= nil then logOnce(ctx, "type:" .. tostring(spec.type), "unknown target type '" .. tostring(spec.type) .. "' ignored") end
    return nil
end

-- OTHER-AREA: {label = quest label, map = the quest's map id} when the last read() found the tracked main quest on another
-- map (read() gave nil then), else nil. A fresh copy each call.
function M.elsewhere()
    local e = S.elsewhere
    if type(e) ~= "table" then return nil end
    return { label = tostring(e.label or ""), map = tostring(e.map or "") }
end

-- ---------- unlocked door fallback ----------
-- doorEvents: list or map of { id = text, pos = {x,y,z} metres (Z up), t = seconds (same clock as now),
--             kind = "door"|"lock" (optional, default "door"), label = text (optional), reached = bool (optional) }
-- A door the player reaches (3 m horizontal, 2.5 m vertical) is remembered as reached and never chosen again.
local function evKey(ev) return tostring(ev.id) .. "@" .. tostring(ev.t) end

local function posOk(p)
    return type(p) == "table" and goodNum(p[1]) and goodNum(p[2]) and goodNum(p[3])
end

local function isReached(p, pl)
    local dx, dy, dz = p[1] - pl[1], p[2] - pl[2], p[3] - pl[3]
    return dx * dx + dy * dy <= REACH_H_M * REACH_H_M and math.abs(dz) <= REACH_V_M
end

local function evOk(ev)
    return type(ev) == "table" and posOk(ev.pos) and goodNum(ev.t) and ev.id ~= nil
end

-- UNLOCK-BATCH: unlock events are grouped into batches by time, not by call: an event whose ev.t is at most BATCH_GAP_S after
-- the newest event of the open batch joins it (doors are read on their own timers, so one mass flip reaches us over
-- several ticks). The batch is decided once no new event came for longer than BATCH_GAP_S (ev.t is the tick time,
-- so a later event can't join any more): 3+ events = a mass flip (story / loading), all ignored for good, one log
-- line; fewer = each gets its map worked out once (GetMapIDByLocation / level name) for the other-map filter and
-- becomes a candidate. Events of the open batch are held (not candidates) until then.
local function closeBatch(ctx, now)
    local list = S.batch
    S.batch, S.batchKey, S.batchLast = {}, {}, nil
    if #list == 0 then return end
    if #list >= MASS_UNLOCK_N then
        for _, f in ipairs(list) do S.ignoredEv[f.key] = true end
        logLine(ctx, "ignored " .. #list .. " unlocks at once (a story or loading change, not a door the player " ..
            "opened)")
        return
    end
    for _, f in ipairs(list) do
        local ok, info = pcall(eventMap, ctx, f.ev, now)
        S.evMap[f.key] = ok and info or nil
    end
end

local function takeNewEvents(doorEvents, now, ctx)
    local fresh = {}
    for _, ev in pairs(doorEvents) do
        -- an event older than FORGET_S can never be a candidate again, so it is not taken in at all: after its key
        -- is forgotten (FORGET_S * 2) main.lua's doorEvents may still hold it, and taking it in again would close a
        -- batch for it (log line / map lookups) on every call
        if evOk(ev) and now - ev.t <= FORGET_S then
            local key = evKey(ev)
            if S.seenEv[key] == nil then
                S.seenEv[key] = ev.t
                fresh[#fresh + 1] = { ev = ev, key = key }
            end
        end
    end
    -- oldest first, so a gap between two new events closes the batch in the right place
    if #fresh > 1 then
        table.sort(fresh, function(a, b)
            if a.ev.t ~= b.ev.t then return a.ev.t < b.ev.t end
            return a.key < b.key
        end)
    end
    for _, f in ipairs(fresh) do
        if S.batchLast ~= nil and f.ev.t - S.batchLast > BATCH_GAP_S then closeBatch(ctx, now) end
        S.batch[#S.batch + 1] = f
        S.batchKey[f.key] = true
        if S.batchLast == nil or f.ev.t > S.batchLast then S.batchLast = f.ev.t end
    end
    if S.batchLast ~= nil and now - S.batchLast > BATCH_GAP_S then closeBatch(ctx, now) end
end

function M.unlockedTarget(doorEvents, playerPos, now, ctx)   -- ctx optional (log, findAll, array)
    if type(doorEvents) ~= "table" then return nil end
    if not goodNum(now) then now = os.clock() end
    local havePlayer = posOk(playerPos)
    -- forget old "reached" marks (and old UNLOCK-BATCH bookkeeping)
    if S.reachedN > 0 then
        for k, at in pairs(S.reached) do
            if now - at > FORGET_S * 2 then S.reached[k] = nil; S.reachedN = S.reachedN - 1 end
        end
    end
    for k, t0 in pairs(S.seenEv) do   -- safe: takeNewEvents never takes an event this old in again
        if now - t0 > FORGET_S * 2 then S.seenEv[k], S.ignoredEv[k], S.evMap[k] = nil, nil, nil end
    end
    takeNewEvents(doorEvents, now, ctx)
    local best = nil
    for _, ev in pairs(doorEvents) do
        if evOk(ev) then
            local key = evKey(ev)
            local age = now - ev.t
            if age <= FORGET_S and not S.reached[key] and ev.reached ~= true and not S.ignoredEv[key] then
                if havePlayer and isReached(ev.pos, playerPos) then
                    S.reached[key] = now
                    S.reachedN = S.reachedN + 1
                    pcall(function() ev.reached = true end)
                elseif not S.batchKey[key]      -- UNLOCK-BATCH: still in the open batch -> held, not a candidate yet
                    and (best == nil or ev.t > best.t or (ev.t == best.t and tostring(ev.id) > tostring(best.id)))
                    and eventOnThisMap(ctx, key, now) then
                    best = ev
                end
            end
        end
    end
    if not best then return nil end
    local kind = (best.kind == "lock" or best.kind == "door") and best.kind or "door"
    return makeTarget(ctx, best.pos, kind, best.id, type(best.label) == "string" and best.label or "", "unlocked")
end

function M.choose(tracked, unlocked)
    if type(tracked) == "table" then return tracked end
    if type(unlocked) == "table" then return unlocked end
    return nil
end

return M
