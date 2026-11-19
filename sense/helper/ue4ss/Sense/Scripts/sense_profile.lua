-- sense_profile: the game profile and the learned file of the Sense helper. Pure Lua, no game touches.
-- Usage: local profile = require("sense_profile")
--   profile.load(dir, game[, log])         -> P, info   profiles\_default.json + profiles\<game>.json, merged
--   profile.merge(base, over)              -> new table  lists are joined, objects merged key by key, the rest
--                                                        (numbers, strings, true/false) of `over` wins; "target" is
--                                                        always replaced as a whole
--   profile.normalize(raw[, log])          -> P          checked profile with lookups (seedByName, doorByName, ...)
--   profile.notItem(P, cls)                -> true|false never an item/door (not_items, not_item_words)
--   profile.cleanRule(rule)                -> rule|nil, why
--   profile.cleanRules(list[, log, where]) -> list (only good rules)
--   profile.readGameKey(dir)               -> "pbz" | nil   first word of <dir>\game.txt (letters, digits, _ -)
--   profile.safeKey(s)                     -> s with only letters, digits, _ and - (or nil)
--   profile.emptyLearned()                 -> {v=1, classes={}, doors={}}
--   profile.cleanLearned(raw[, log])       -> L
--   profile.learnedPaths(dir, game)        -> main path, spare path
--   profile.loadLearned(dir, game[, log])  -> L, info
--   profile.saveLearned(dir, game, L[, log]) -> true, path | false, err, busy
--   profile.learnedCount(L)                -> n  (learned item classes + learned door classes)
--   profile.setLog(fn)                     -> log lines go to fn(text) (default: print "[Sense] ...")
--
-- Profile file shape (unknown keys are ignored):
--   { "game", "discovery":{mode, min_interval_s, move_m, budget_s, max_interval_s},
--     "item_words", "collectible_words", "door_words", "exclude_words", "not_items", "not_item_words",
--     "guess_gone":[RULE...], "seeds":[{name, kind, require:[RULE], gone:[RULE]}],
--     "doors":[{name, unlocked:RULE, pos_prop, id_prop, kind:"lock"|"door"}], "target":{type...}|null, "player_hint", "fov_axis" }
-- Word lists are matched in any case at the START of a name part ("MapLockObject" -> Map|Lock|Object, so "lock"
-- hits it but not "Block"). not_items are exact class names. not_item_words are exact-case pieces anywhere in the
-- class name (like the old main.lua NOT_ITEM_WORDS).
-- Missing or broken files never stop the helper: built-in defaults + one log line.

local M = {}

M.KINDS = { pickup = true, collectible = true }
M.RULE_TYPES = { hidden = true, bool = true, arrayAnyTrue = true, call = true, nameSet = true, vanish = true,
                 collision = true, draw = true, num = true }   -- "num" (a number property equals is) is only written
                                                            -- by learning (sense_state), never needed in a profile
M.ARRAY_MAX = 64          -- arrayAnyTrue never looks at more than this many entries
M.MIN_INTERVAL_FLOOR = 10 -- discovery never runs more often than this, whatever a profile says

-- built-in defaults (the same as profiles\_default.json; used when that file is missing or broken)
M.DEFAULTS = {
    game = "_default",
    discovery = { mode = "auto", min_interval_s = 120, move_m = 200, budget_s = 0.05, max_interval_s = 600 },
    item_words = { "pickup", "item", "loot", "collect", "treasure", "gift", "reward", "chest", "bonus", "gather" },
    collectible_words = { "treasure", "chest", "collect", "bonus" },
    door_words = { "door", "gate", "lock", "barrier", "blocker", "shutter", "portcullis" },
    exclude_words = { "Spawn", "Manager", "Subsystem", "Widget", "UI", "HUD", "Volume", "Trigger", "Cache",
                      "Default__", "Character", "Pawn", "Controller", "Camera", "Light", "Sound", "Audio", "Decal",
                      "Effect", "FX", "VFX", "Niagara", "Particle", "Emitter", "Spline", "Projectile", "Shop",
                      "Vendor", "Merchant", "Breakable", "Destructible", "Geometry", "Preview", "Debug", "Marker",
                      "Icon", "Sequence", "Material" },
    not_items = {},
    not_item_words = {},
    guess_gone = { { type = "hidden" } },
    seeds = {},
    doors = {},
    target = nil,
    player_hint = nil,
    fov_axis = "v",
}

-- built-in game profiles, used only when profiles\<game>.json is missing or broken. A copy of
-- profiles\pbz.json, so PBZ keeps its seeds, not_items and doors and never runs the full-world discovery sweep
-- (the ~0.1 s hitch). Discovery is always forced off for a built-in profile. Keep in step with profiles\pbz.json.
local function boolRule(p, v) return { type = "bool", prop = p, is = v } end
local PBZ_LOCK = function(name)
    return { name = name, kind = "lock", unlocked = boolRule("bUnlocked", true), pos_prop = "ActorLocation", id_prop = "GuidString" }
end
M.BUILTIN = {
    pbz = {
        game = "pbz",
        discovery = { mode = "off" },
        not_items = { "BP_FlowInteractActor_Character_C", "BP_FlowInteractActor_Female_ScareCrow1_C",
                      "BP_FlowInteract_HuanShuPingZhang_C", "TriggerBox", "PBZSaveTriggerBox", "HDRIBackdrop_C",
                      "GeometryCollectionActor", "BP_ChestSwipeMaterialCache_C", "BP_InteractiveWaterTriger_C" },
        not_item_words = { "ChaoSpawner", "ChaosSpawner" },
        exclude_words = { "ChaoSpawner", "ChaosSpawner", "FogDoor", "MapTargetPoint" },
        seeds = {
            { name = "BP_Chest_C", kind = "collectible",
              gone = { { type = "hidden" }, boolRule("CurSwitchState", true),
                       { type = "arrayAnyTrue", prop = "bInteracted", max = 64 } } },
            { name = "BP_FlowInteract_MemoryFlower_C", kind = "collectible", gone = { { type = "hidden" } } },
            { name = "BP_HangingGift_C", kind = "pickup",
              gone = { { type = "hidden" }, { type = "call", fn = "IsValidPickup", is = false } } },
            { name = "BP_FlowInteractActor_C", kind = "pickup",
              require = { { type = "nameSet", prop = "LootItemID" } },
              gone = { { type = "hidden" }, { type = "call", fn = "IsValidPickup", is = false } } },
            { name = "BP_Rubbish_C", kind = "pickup",
              gone = { { type = "hidden" }, boolRule("bDestroyed", true), { type = "call", fn = "IsValidPickup", is = false } } },
        },
        doors = { PBZ_LOCK("MapLockObject"), PBZ_LOCK("SwitchLockObject"), PBZ_LOCK("ShaChiWeaponLockObject"),
                  PBZ_LOCK("DoorDirLockObject") },
        target = { type = "pbz_narrative" },
    },
}

-- ---------- log ----------
local logFn = function(s) print("[Sense] " .. tostring(s) .. "\n") end
function M.setLog(fn) if type(fn) == "function" then logFn = fn end end
local function say(log, s)
    local f = type(log) == "function" and log or logFn
    pcall(f, s)
end

-- ---------- other modules (loaded when first needed; a missing one is not cached so a later call can retry) ----------
local deps = {}
local function dep(name)
    if deps[name] then return deps[name] end
    local ok, m = pcall(require, name)
    if ok and type(m) == "table" then deps[name] = m; return m end
    return nil
end
function M._setDep(name, mod) deps[name] = mod end   -- tests

-- ---------- small helpers ----------
local function isNull(v)
    if type(v) ~= "table" then return false end
    local json = deps["sense_json"]
    if json and v == json.NULL then return true end
    local mt = getmetatable(v)
    return type(mt) == "table" and mt.__name == "null"
end

-- a table whose keys are exactly 1..n (an empty table counts as a list too)
local function isList(t)
    if type(t) ~= "table" or isNull(t) then return false end
    local n = 0
    for k in pairs(t) do
        if math.type(k) ~= "integer" or k < 1 then return false end
        n = n + 1
    end
    return n == #t
end
M.isList = isList

local function copy(v, depth)
    depth = depth or 0
    if type(v) ~= "table" or isNull(v) or depth > 32 then return v end
    local out = {}
    for k, x in pairs(v) do out[k] = copy(x, depth + 1) end
    return out
end
M.copy = copy

local function finitePos(n) return type(n) == "number" and n == n and n > 0 and n ~= math.huge end

function M.safeKey(s)
    if type(s) ~= "string" then return nil end
    s = s:gsub("[^%w_%-]", "")
    if s == "" then return nil end
    return s:sub(1, 64)
end

-- ---------- merge ----------
local REPLACE = { target = true }   -- keys whose value is always taken whole from the game profile

function M.merge(base, over)
    local out = copy(type(base) == "table" and base or {})
    if type(over) ~= "table" or isNull(over) then return out end
    for k, v in pairs(over) do
        local b = out[k]
        if isNull(v) then
            out[k] = nil                                              -- explicit null = "none"
        elseif REPLACE[k] or type(v) ~= "table" or type(b) ~= "table" or isNull(b) then
            out[k] = copy(v)
        elseif isList(b) and isList(v) then
            for _, x in ipairs(v) do b[#b + 1] = copy(x) end          -- lists are joined
        elseif not isList(b) or next(b) == nil then
            if isList(v) and next(v) ~= nil then out[k] = copy(v)     -- an object replaced by a list
            else out[k] = M.merge(b, v) end                           -- objects merged key by key
        else
            out[k] = copy(v)                                          -- a list replaced by an object
        end
    end
    return out
end

-- ---------- rules ----------
function M.cleanRule(r)
    if type(r) ~= "table" or isNull(r) then return nil, "not an object" end
    local t = r.type
    if type(t) ~= "string" or not M.RULE_TYPES[t] then return nil, "unknown rule type " .. tostring(t) end
    if t == "vanish" then return { type = t } end
    if t == "hidden" then
        if r.is == nil then return { type = t } end
        if type(r.is) ~= "boolean" then return nil, "hidden rule: is must be true/false" end
        return { type = t, is = r.is }
    end
    if t == "num" then
        if type(r.prop) ~= "string" or r.prop == "" then return nil, "num rule without prop" end
        if type(r.is) ~= "number" or r.is ~= r.is or r.is == math.huge or r.is == -math.huge then
            return nil, "num rule needs is = a number"
        end
        return { type = t, prop = r.prop, is = r.is }
    end
    if t == "bool" then
        if type(r.prop) ~= "string" or r.prop == "" then return nil, "bool rule without prop" end
        if type(r.is) ~= "boolean" then return nil, "bool rule needs is = true/false" end
        return { type = t, prop = r.prop, is = r.is }
    end
    if t == "arrayAnyTrue" then
        if type(r.prop) ~= "string" or r.prop == "" then return nil, "arrayAnyTrue rule without prop" end
        local mx = M.ARRAY_MAX
        if type(r.max) == "number" and r.max == r.max then mx = math.floor(math.max(1, math.min(M.ARRAY_MAX, r.max))) end
        return { type = t, prop = r.prop, max = mx }
    end
    if t == "call" then
        if type(r.fn) ~= "string" or r.fn == "" then return nil, "call rule without fn" end
        local v = r.is
        if type(v) ~= "boolean" and type(v) ~= "number" and type(v) ~= "string" then
            return nil, "call rule needs is = a plain value"
        end
        return { type = t, fn = r.fn, is = v }
    end
    if t == "nameSet" then
        if type(r.prop) ~= "string" or r.prop == "" then return nil, "nameSet rule without prop" end
        return { type = t, prop = r.prop }
    end
    -- collision / draw
    local is = false
    if type(r.is) == "boolean" then is = r.is elseif r.is ~= nil then return nil, t .. " rule needs is = true/false" end
    return { type = t, is = is }
end

function M.cleanRules(list, log, where)
    local out = {}
    if type(list) ~= "table" or isNull(list) then return out end
    if not isList(list) then
        local one = M.cleanRule(list)          -- a single rule written without [ ]
        if one then out[1] = one end
        return out
    end
    for _, r in ipairs(list) do
        local c, why = M.cleanRule(r)
        if c then out[#out + 1] = c
        elseif log ~= false then say(log, "profile: skipped a rule in " .. tostring(where) .. ": " .. tostring(why)) end
    end
    return out
end

-- ---------- normalize ----------
local function wordList(v)
    local out, seen = {}, {}
    if type(v) ~= "table" or isNull(v) then return out end
    for _, w in ipairs(v) do
        if type(w) == "string" and w ~= "" and not seen[w] then seen[w] = true; out[#out + 1] = w end
    end
    return out
end

local function lowerList(list)
    local out, seen = {}, {}
    for _, w in ipairs(list) do
        local l = w:lower()
        if not seen[l] then seen[l] = true; out[#out + 1] = l end
    end
    return out
end

local function num(v, default, minv)
    if finitePos(v) or (v == 0 and minv == 0) then
        if minv and v < minv then return minv end
        return v
    end
    return default
end

function M.normalize(raw, log)
    raw = type(raw) == "table" and raw or {}
    local D = M.DEFAULTS
    local P = {}
    P.game = M.safeKey(raw.game) or "_default"
    -- discovery
    local d = type(raw.discovery) == "table" and raw.discovery or {}
    local mode = type(d.mode) == "string" and d.mode:lower() or D.discovery.mode
    if mode ~= "auto" and mode ~= "off" then
        say(log, "profile: discovery mode '" .. tostring(d.mode) .. "' is unknown, using off")
        mode = "off"
    end
    local minI = num(d.min_interval_s, D.discovery.min_interval_s, M.MIN_INTERVAL_FLOOR)
    local maxI = num(d.max_interval_s, D.discovery.max_interval_s)
    if maxI < minI then maxI = minI end
    P.discovery = { mode = mode, min_interval_s = minI, move_m = num(d.move_m, D.discovery.move_m),
                    budget_s = num(d.budget_s, D.discovery.budget_s), max_interval_s = maxI }
    -- words
    P.item_words = wordList(raw.item_words)
    P.collectible_words = wordList(raw.collectible_words)
    P.door_words = wordList(raw.door_words)
    P.exclude_words = wordList(raw.exclude_words)
    P.not_items = wordList(raw.not_items)
    P.not_item_words = wordList(raw.not_item_words)
    P.itemWordsL = lowerList(P.item_words)
    P.collectibleWordsL = lowerList(P.collectible_words)
    P.doorWordsL = lowerList(P.door_words)
    P.excludeWordsL = lowerList(P.exclude_words)
    P.notItemSet = {}
    for _, n in ipairs(P.not_items) do P.notItemSet[n] = true end
    -- rules for classes guessed from words (until something better is learned)
    if raw.guess_gone == nil then P.guess_gone = copy(D.guess_gone)
    else P.guess_gone = M.cleanRules(raw.guess_gone, log, "guess_gone") end
    -- seeds (a later seed with the same name replaces the earlier one, keeping its place in the order)
    P.seeds, P.seedByName = {}, {}
    if type(raw.seeds) == "table" and not isNull(raw.seeds) then
        for i, s in ipairs(raw.seeds) do
            local name = type(s) == "table" and s.name or nil
            local kind = type(s) == "table" and s.kind or nil
            if type(name) ~= "string" or name == "" then
                say(log, "profile: seed " .. i .. " has no name, skipped")
            elseif not M.KINDS[kind] then
                say(log, "profile: seed " .. name .. " has kind '" .. tostring(kind) .. "' (needs pickup or collectible), skipped")
            else
                local e = { name = name, kind = kind,
                            require = M.cleanRules(s.require, log, "seed " .. name .. " require"),
                            gone = M.cleanRules(s.gone, log, "seed " .. name .. " gone"), seed = true }
                local old = P.seedByName[name]
                if old then
                    for j, x in ipairs(P.seeds) do if x == old then P.seeds[j] = e end end
                else
                    P.seeds[#P.seeds + 1] = e
                end
                P.seedByName[name] = e
            end
        end
    end
    -- doors
    P.doors, P.doorByName = {}, {}
    if type(raw.doors) == "table" and not isNull(raw.doors) then
        for i, s in ipairs(raw.doors) do
            local name = type(s) == "table" and s.name or nil
            if type(name) ~= "string" or name == "" then
                say(log, "profile: door " .. i .. " has no name, skipped")
            else
                local unlocked = nil
                if s.unlocked ~= nil then
                    local why
                    unlocked, why = M.cleanRule(s.unlocked)
                    if not unlocked then say(log, "profile: door " .. name .. " unlocked rule skipped: " .. tostring(why)) end
                end
                -- kind = what a target from this door is called ("lock" | "door"); nil = sense_state guesses it from
                -- the class name. door = true marks every door entry.
                local dk = (s.kind == "lock" or s.kind == "door") and s.kind or nil
                local e = { name = name, kind = dk, door = true, unlocked = unlocked,
                            pos_prop = type(s.pos_prop) == "string" and s.pos_prop ~= "" and s.pos_prop or nil,
                            id_prop = type(s.id_prop) == "string" and s.id_prop ~= "" and s.id_prop or nil,
                            seed = true }
                local old = P.doorByName[name]
                if old then
                    for j, x in ipairs(P.doors) do if x == old then P.doors[j] = e end end
                else
                    P.doors[#P.doors + 1] = e
                end
                P.doorByName[name] = e
            end
        end
    end
    -- target, player hint, fov axis
    if type(raw.target) == "table" and not isNull(raw.target) and type(raw.target.type) == "string" then
        P.target = copy(raw.target)
    else
        P.target = nil
    end
    P.player_hint = type(raw.player_hint) == "string" and raw.player_hint ~= "" and raw.player_hint or nil
    P.fov_axis = (raw.fov_axis == "h" or raw.fov_axis == "v") and raw.fov_axis or D.fov_axis
    return P
end

function M.notItem(P, cls)
    if type(cls) ~= "string" or type(P) ~= "table" then return false end
    if P.notItemSet and P.notItemSet[cls] then return true end
    for _, w in ipairs(P.not_item_words or {}) do
        if cls:find(w, 1, true) then return true end
    end
    return false
end

-- ---------- load ----------
function M.readGameKey(dir)
    if type(dir) ~= "string" then return nil end
    local files = dep("sense_files")
    if not files then return nil end
    local text = files.readAll(dir .. "\\game.txt")
    if type(text) ~= "string" then return nil end
    text = text:gsub("^\239\187\191", "")
    local w = text:match("^%s*(%S+)")
    local key = M.safeKey(w)
    return key and key:lower() or nil
end

-- P, info. info = {default = "file"|"built-in", game = "file"|"none"|"broken", errors = {...}}
function M.load(dir, game, log)
    local info = { default = "built-in", game = "none", errors = {} }
    local files = dep("sense_files")
    local base = M.DEFAULTS
    local key = M.safeKey(game)
    if key then key = key:lower() end
    if not files then
        info.errors[#info.errors + 1] = "sense_files missing"
        say(log, "profile: sense_files.lua could not be loaded; using built-in defaults only")
    elseif type(dir) ~= "string" or dir == "" then
        info.errors[#info.errors + 1] = "no folder"
        say(log, "profile: no mod folder given; using built-in defaults only")
    else
        local okd, rawD, errD = pcall(files.loadJson, dir .. "\\profiles\\_default.json")
        if okd and type(rawD) == "table" and not isList(rawD) then
            base = rawD
            info.default = "file"
        else
            local e = okd and (errD or "not a JSON object") or tostring(rawD)
            info.errors[#info.errors + 1] = e
            say(log, "profile: _default.json not used (" .. tostring(e) .. "); using built-in defaults")
        end
        if key and key ~= "_default" then
            local okg, rawG, errG = pcall(files.loadJson, dir .. "\\profiles\\" .. key .. ".json")
            if okg and type(rawG) == "table" and not isList(rawG) then
                info.game = "file"
                local merged = M.merge(base, rawG)
                local P = M.normalize(merged, log)
                P.game = key
                P.info = info
                return P, info
            end
            local e = okg and (errG or "not a JSON object") or tostring(rawG)
            info.game = (type(e) == "string" and e:find("cannot open", 1, true)) and "none" or "broken"
            info.errors[#info.errors + 1] = e
            say(log, "profile: " .. key .. ".json not used (" .. tostring(e) .. ")" ..
                (M.BUILTIN[key] and "" or "; the helper guesses from names only"))
        end
    end
    return M.fallback(key, base, info, log)
end

-- the profile when profiles\<game>.json could not be used: the built-in profile of that game (M.BUILTIN, discovery
-- forced off, one log line) or else the defaults alone. Never raises.
function M.fallback(game, base, info, log)
    info = type(info) == "table" and info or { default = "built-in", game = "none", errors = {} }
    local key = M.safeKey(game)
    if key then key = key:lower() end
    if type(base) ~= "table" then base = M.DEFAULTS end
    local builtin = key and M.BUILTIN[key] or nil
    if builtin then
        local okb, P = pcall(function() return M.normalize(M.merge(base, builtin), log) end)
        if okb and type(P) == "table" then
            P.discovery.mode = "off"
            P.game = key
            info.builtin = true
            P.info = info
            say(log, "profile: using the built-in " .. key .. " profile (" .. #P.seeds .. " seeds, " .. #P.doors ..
                " doors, discovery off)")
            return P, info
        end
    end
    local P = M.normalize(M.merge(base, {}), log)
    P.game = key or P.game
    P.info = info
    return P, info
end

-- ---------- learned file ----------
function M.emptyLearned() return { v = 1, classes = {}, doors = {} } end

local function scalarExtras(src, dst, known)
    for k, v in pairs(src) do
        if not known[k] and type(k) == "string" and (type(v) == "string" or type(v) == "number" or type(v) == "boolean") then
            dst[k] = v
        end
    end
end

local CLASS_KEYS = { kind = true, gone = true, seen = true, first = true }
local DOOR_KEYS = { unlocked = true, seen = true, first = true }

function M.cleanLearned(raw, log)
    local L = M.emptyLearned()
    if type(raw) ~= "table" or isNull(raw) then return L end
    if type(raw.classes) == "table" and not isNull(raw.classes) then
        for cls, e in pairs(raw.classes) do
            if type(cls) == "string" and cls ~= "" and type(e) == "table" and M.KINDS[e.kind] then
                local c = { kind = e.kind, gone = M.cleanRules(e.gone, false),
                            seen = (type(e.seen) == "number" and e.seen >= 0) and math.floor(e.seen) or 0,
                            first = type(e.first) == "string" and e.first or nil }
                scalarExtras(e, c, CLASS_KEYS)
                L.classes[cls] = c
            end
        end
    end
    if type(raw.doors) == "table" and not isNull(raw.doors) then
        for cls, e in pairs(raw.doors) do
            if type(cls) == "string" and cls ~= "" and type(e) == "table" then
                local r = M.cleanRule(e.unlocked)
                -- a learned "vanish" door rule is dropped. Learning never writes one any more, and an old one
                -- would fire "unlocked" whenever the door's level part streams out (then the class can learn anew)
                if r and r.type == "vanish" then
                    say(log, "learned: dropped the old 'vanish' unlocked rule of door class " .. cls)
                    r = nil
                end
                if r then
                    local c = { unlocked = r, seen = (type(e.seen) == "number" and e.seen >= 0) and math.floor(e.seen) or 0,
                                first = type(e.first) == "string" and e.first or nil }
                    scalarExtras(e, c, DOOR_KEYS)
                    L.doors[cls] = c
                end
            end
        end
    end
    return L
end

function M.learnedPaths(dir, game)
    local key = M.safeKey(game) or "_unknown"
    key = key:lower()
    dir = type(dir) == "string" and dir or "."
    return dir .. "\\learned\\" .. key .. ".json", dir .. "\\out\\learned_" .. key .. ".json"
end

-- L, info. info = {path = where it came from | nil, err = text | nil}. Never nil: a missing/broken file gives an
-- empty L. The main path is learned\<game>.json; out\learned_<game>.json is the spare (used when learned\ is missing).
function M.loadLearned(dir, game, log)
    local files = dep("sense_files")
    if not files then
        say(log, "learned: sense_files.lua could not be loaded; starting with nothing learned")
        return M.emptyLearned(), { err = "sense_files missing" }
    end
    local main, spare = M.learnedPaths(dir, game)
    local errs = {}
    for _, p in ipairs({ main, spare }) do
        local ok, raw, err = pcall(files.loadJson, p)
        if ok and type(raw) == "table" and not isList(raw) then
            local L = M.cleanLearned(raw, log)
            local nd = 0
            for _ in pairs(L.doors) do nd = nd + 1 end
            say(log, string.format("learned: %d class rule(s), %d door rule(s) from %s", M.learnedCount(L) - nd, nd, p))
            return L, { path = p }
        end
        errs[#errs + 1] = ok and tostring(err or "not a JSON object") or tostring(raw)
    end
    -- a missing file is normal on the first start; a broken one is worth a line too
    say(log, "learned: nothing loaded (" .. table.concat(errs, "; ") .. "); starting with nothing learned")
    return M.emptyLearned(), { err = errs[1] }
end

local function jsonReady(L, json)
    local arr = (json and json.array) or function(t) return t end
    local out = { v = 1, classes = {}, doors = {} }
    for cls, e in pairs(type(L.classes) == "table" and L.classes or {}) do
        if type(cls) == "string" and type(e) == "table" and M.KINDS[e.kind] then
            local c = { kind = e.kind, gone = arr(M.cleanRules(e.gone, false)),
                        seen = type(e.seen) == "number" and e.seen or 0, first = type(e.first) == "string" and e.first or nil }
            scalarExtras(e, c, CLASS_KEYS)
            out.classes[cls] = c
        end
    end
    for cls, e in pairs(type(L.doors) == "table" and L.doors or {}) do
        local r = type(e) == "table" and M.cleanRule(e.unlocked) or nil
        if type(cls) == "string" and r then
            local c = { unlocked = r, seen = type(e.seen) == "number" and e.seen or 0,
                        first = type(e.first) == "string" and e.first or nil }
            scalarExtras(e, c, DOOR_KEYS)
            out.doors[cls] = c
        end
    end
    return out
end

function M.saveLearned(dir, game, L, log)
    if type(L) ~= "table" then return false, "nothing to save", false end
    local files = dep("sense_files")
    if not files then return false, "sense_files missing", false end
    local json = dep("sense_json")
    local data = jsonReady(L, json)
    local main, spare = M.learnedPaths(dir, game)
    local ok, a, b, c = pcall(files.saveJson, main, data)
    if ok and a == true then return true, main end
    local err, busy = ok and b or tostring(a), ok and c or false
    if busy then return false, err, true end            -- the file is there, just held open: try again later
    -- learned\ missing (Lua cannot make folders): use the spare in out\
    local ok2, a2, b2, c2 = pcall(files.saveJson, spare, data)
    if ok2 and a2 == true then return true, spare end
    local err2 = ok2 and b2 or tostring(a2)
    say(log, "learned: could not save (" .. tostring(err) .. "; " .. tostring(err2) .. ")")
    return false, tostring(err) .. "; " .. tostring(err2), ok2 and c2 or false
end

function M.learnedCount(L)
    local n = 0
    if type(L) ~= "table" then return 0 end
    for _ in pairs(type(L.classes) == "table" and L.classes or {}) do n = n + 1 end
    for _ in pairs(type(L.doors) == "table" and L.doors or {}) do n = n + 1 end
    return n
end

return M
