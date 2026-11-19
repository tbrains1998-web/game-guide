-- sense_re/profile.lua  (Game Guide Sense helper for RE Engine games)
-- Loads the per-game profile and merges it over the defaults.
--   files (relative to <game>\reframework\data, read with json.load_file):
--     sense_re/profiles/_default.json   word lists + discovery defaults
--     sense_re/profiles/<game>.json     seeds, doors, target, player_hint, fov_axis (all optional)
--   A missing or broken file never crashes: built-in defaults are used and ONE log line is written.
-- Read-only: this module only reads JSON files. It never touches game objects.
-- Public API (all return plain Lua tables, no game objects):
--   M.game_key()                 -> "re9" | "dd2" | ... | "unknown"   (data/sense_re/game.txt written by the
--                                   installer, read with pcall(fs.read); else reframework:get_game_name())
--   M.load(game, log)            -> P, info   (P = merged profile, info = {default=bool, game=bool, notes={...}})
--   M.merge(def, prof, notes)    -> P         (pure; def/prof may be nil or junk)
--   M.normalize_rule(rule)       -> rule|nil  (only the known RULE types)
--   M.profile_path(game), M.default_path(), M.learned_path(game)
--   M.DEFAULTS                   built-in defaults (used when _default.json is missing/broken)

local M = {}

M.DIR = "sense_re/profiles/"
M.LEARNED_DIR = "sense_re/learned/"

local RULE_TYPES = {
    hidden = true, bool = true, arrayAnyTrue = true, call = true,
    nameSet = true, vanish = true, collision = true, draw = true,
}
local KINDS = { pickup = true, collectible = true }
local WORD_KEYS = { "item_words", "collectible_words", "door_words", "exclude_words", "not_items" }

M.DEFAULTS = {
    discovery = { mode = "auto", min_interval_s = 120, move_m = 200, budget_s = 0.05, max_interval_s = 600 },
    item_words = { "Item", "Pickup", "Loot", "Drop" },
    collectible_words = { "Chest", "TreasureBox", "Treasure", "Safe", "Locker", "Collect" },
    door_words = { "Door", "Gate", "Shutter", "Lock", "Barricade" },
    exclude_words = { "Param", "Data", "Setting", "Manager", "Gui", "Sound", "Effect", "Driver",
                      "BehaviorTree", "Track", "Clock", "LockOn", "Lockon", "Catalog", "Icon",
                      "Character", "Human", "Npc", "Enemy" },
    not_items = {},
    fov_axis = "v",
}

local function is_str(v) return type(v) == "string" and v ~= "" end
local function is_num(v) return type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge end

function M.profile_path(game) return M.DIR .. tostring(game) .. ".json" end
function M.default_path() return M.DIR .. "_default.json" end
function M.learned_path(game) return M.LEARNED_DIR .. tostring(game) .. ".json" end

-- game key: lower case, only [a-z0-9_-]
local function clean_key(s)
    if type(s) ~= "string" then return nil end
    s = s:lower():gsub("[^a-z0-9_%-]", "")   -- ASCII only (%w can match high bytes such as a BOM in some locales)
    if s == "" then return nil end
    return s
end
M.clean_key = clean_key

M.GAME_FILE = "sense_re/game.txt"   -- written by Game Guide's installer (install_helper.py, data\sense_re\game.txt)

-- the key the installer wrote, or nil (missing / empty / unreadable file)
function M.game_file_key()
    local f = rawget(_G, "fs")
    if type(f) ~= "table" or type(f.read) ~= "function" then return nil end
    local ok, txt = pcall(f.read, M.GAME_FILE)
    if not ok or type(txt) ~= "string" or txt == "" then return nil end
    txt = txt:sub(1, 200):match("^[^\r\n]*") or ""          -- first line only
    return clean_key(txt)
end

-- game key: data/sense_re/game.txt first, else reframework:get_game_name()
function M.game_key()
    local fk = M.game_file_key()
    if fk then return fk end
    local rf = rawget(_G, "reframework")
    if rf == nil then return "unknown" end
    local ok, name = pcall(function() return rf:get_game_name() end)
    if not ok or type(name) ~= "string" then
        ok, name = pcall(function() return rf.get_game_name() end)
    end
    return (ok and clean_key(name)) or "unknown"
end

-- a JSON list of strings -> dense Lua list (drops anything that is not a non-empty string)
local function str_list(v)
    local out = {}
    if type(v) ~= "table" then return out end
    for i = 1, #v do
        if is_str(v[i]) then out[#out + 1] = v[i] end
    end
    return out
end

local function union(a, b)
    local out, seen = {}, {}
    for _, list in ipairs({ a, b }) do
        for i = 1, #list do
            local w = list[i]
            if not seen[w] then seen[w] = true; out[#out + 1] = w end
        end
    end
    return out
end

local function to_set(list)
    local s = {}
    for i = 1, #list do s[list[i]] = true end
    return s
end

function M.normalize_rule(r)
    if type(r) ~= "table" or not RULE_TYPES[r.type] then return nil end
    local t = r.type
    local out = { type = t }
    if t == "bool" then
        if not is_str(r.prop) or type(r.is) ~= "boolean" then return nil end
        out.prop, out.is = r.prop, r.is
    elseif t == "arrayAnyTrue" then
        if not is_str(r.prop) then return nil end
        local mx = is_num(r.max) and math.floor(r.max) or 64
        if mx < 1 then mx = 1 end
        if mx > 64 then mx = 64 end
        out.prop, out.max = r.prop, mx
    elseif t == "call" then
        if not is_str(r.fn) then return nil end
        local iv = r.is
        if type(iv) ~= "boolean" and type(iv) ~= "number" and type(iv) ~= "string" then return nil end
        out.fn, out.is = r.fn, iv
    elseif t == "nameSet" then
        if not is_str(r.prop) then return nil end
        out.prop = r.prop
    elseif t == "collision" or t == "draw" then
        out.is = (r.is == true)          -- default false (= "drawn/collides no more")
    end
    return out
end

local function rule_list(v, notes, where)
    local out = {}
    if type(v) ~= "table" then return out end
    for i = 1, #v do
        local r = M.normalize_rule(v[i])
        if r then out[#out + 1] = r
        else notes[#notes + 1] = "bad rule dropped in " .. where end
    end
    return out
end

local function seed_list(v, notes)
    local out, by = {}, {}
    if type(v) ~= "table" then return out, by end
    for i = 1, #v do
        local s = v[i]
        if type(s) == "table" and is_str(s.name) and KINDS[s.kind] then
            local e = { name = s.name, kind = s.kind,
                        require = rule_list(s.require, notes, s.name .. ".require"),
                        gone = rule_list(s.gone, notes, s.name .. ".gone") }
            if is_str(s.parent) then e.parent = s.parent end
            if not by[e.name] then out[#out + 1] = e; by[e.name] = e end
        else
            notes[#notes + 1] = "bad seed #" .. i .. " dropped"
        end
    end
    return out, by
end

local function door_list(v, notes)
    local out, by = {}, {}
    if type(v) ~= "table" then return out, by end
    for i = 1, #v do
        local d = v[i]
        if type(d) == "table" and is_str(d.name) then
            local e = { name = d.name }
            if d.unlocked ~= nil then
                e.unlocked = M.normalize_rule(d.unlocked)
                if not e.unlocked and type(d.unlocked) == "table" then
                    notes[#notes + 1] = "bad unlocked rule dropped in " .. d.name
                end
            end
            if is_str(d.pos_prop) then e.pos_prop = d.pos_prop end
            if is_str(d.id_prop) then e.id_prop = d.id_prop end
            if not by[e.name] then out[#out + 1] = e; by[e.name] = e end
        else
            notes[#notes + 1] = "bad door #" .. i .. " dropped"
        end
    end
    return out, by
end

-- re_path steps: each step is exactly one of
--   {"single":"app.X"}  {"call":"get_X"}  {"field":"_x"}  {"item":n}
local function step_ok(s)
    if type(s) ~= "table" then return false end
    local n = 0
    if s.single ~= nil then if not is_str(s.single) then return false end; n = n + 1 end
    if s.call ~= nil then if not is_str(s.call) then return false end; n = n + 1 end
    if s.field ~= nil then if not is_str(s.field) then return false end; n = n + 1 end
    if s.item ~= nil then if not is_num(s.item) or s.item < 0 then return false end; n = n + 1 end
    return n == 1
end

local function steps_ok(steps)
    if type(steps) ~= "table" or #steps == 0 or #steps > 16 then return false end
    if type(steps[1]) ~= "table" or not is_str(steps[1].single) then return false end   -- must start at a singleton
    for i = 1, #steps do if not step_ok(steps[i]) then return false end end
    return true
end
M.steps_ok = steps_ok

local function target_spec(v, notes)
    if type(v) ~= "table" or v.type ~= "re_path" then return nil end
    local paths = {}
    if steps_ok(v.steps) then paths[#paths + 1] = v.steps
    elseif v.steps ~= nil and type(v.steps) == "table" then notes[#notes + 1] = "bad target steps dropped" end
    if type(v.paths) == "table" then
        for i = 1, #v.paths do
            if steps_ok(v.paths[i]) then paths[#paths + 1] = v.paths[i]
            else notes[#notes + 1] = "bad target path #" .. i .. " dropped" end
        end
    end
    if #paths == 0 then return nil end
    local kind = (v.kind == "quest" or v.kind == "lock" or v.kind == "door" or v.kind == "pin") and v.kind or "quest"
    return { type = "re_path", paths = paths, kind = kind,
             label = is_str(v.label) and v.label or "",
             id = is_str(v.id) and v.id or "tracked" }
end

function M.merge(def, prof, notes)
    notes = notes or {}
    if type(def) ~= "table" then def = M.DEFAULTS end
    if type(prof) ~= "table" then prof = {} end
    local P = {}
    P.game = is_str(prof.game) and prof.game or nil
    -- discovery: per key, numbers only (mode: "auto"|"off")
    local d = {}
    for k, v in pairs(M.DEFAULTS.discovery) do d[k] = v end
    local srcs = { def.discovery, prof.discovery }
    for si = 1, 2 do
        local src = srcs[si]
        if type(src) == "table" then
            for k, v in pairs(M.DEFAULTS.discovery) do
                if k == "mode" then
                    if src.mode == "auto" or src.mode == "off" then d.mode = src.mode end
                elseif is_num(src[k]) and src[k] >= 0 then d[k] = src[k] end
            end
        end
    end
    P.discovery = d
    -- word lists: defaults + game additions (game can set "replace_words": true to use only its own)
    for _, k in ipairs(WORD_KEYS) do
        local base = (def[k] ~= nil) and str_list(def[k]) or str_list(M.DEFAULTS[k])
        if prof.replace_words == true and prof[k] ~= nil then base = {} end
        P[k] = union(base, str_list(prof[k]))
    end
    P.not_items_set = to_set(P.not_items)
    P.seeds, P.seed_by_name = seed_list(prof.seeds, notes)
    P.doors, P.door_by_name = door_list(prof.doors, notes)
    P.base_types = union(str_list(def.base_types), str_list(prof.base_types))
    P.target = target_spec(prof.target, notes)
    P.player_hint = is_str(prof.player_hint) and prof.player_hint or nil
    local fa = prof.fov_axis
    if fa ~= "v" and fa ~= "h" then fa = def.fov_axis end
    if fa ~= "v" and fa ~= "h" then fa = "v" end
    P.fov_axis = fa
    -- number keys world.lua / out.lua read (merge must keep them)
    local function numIn(v, lo, hi)
        if type(v) == "number" and v == v and v >= lo and v <= hi then return v end
        return nil
    end
    P.player_foot_dz = numIn(prof.player_foot_dz, -5, 5) or numIn(def.player_foot_dz, -5, 5)
    P.units_per_m = numIn(prof.units_per_m, 0.001, 1000) or numIn(def.units_per_m, 0.001, 1000)
    P.range = numIn(prof.range, 1, 500) or numIn(def.range, 1, 500)
    -- boolean switches learn.lua / out.lua read: game profile wins, then _default.json;
    -- anything that is not a boolean is ignored (learn: draw_gone defaults to on, out: debug_state to off)
    local function boolIn(a, b)
        if type(a) == "boolean" then return a end
        if type(b) == "boolean" then return b end
        return nil
    end
    P.draw_gone = boolIn(prof.draw_gone, def.draw_gone)
    P.debug_state = boolIn(prof.debug_state, def.debug_state)
    return P
end

local function load_json(path)
    local j = rawget(_G, "json")
    if j == nil or type(j.load_file) ~= "function" then return nil end
    local ok, t = pcall(j.load_file, path)
    if ok and type(t) == "table" then return t end
    return nil
end
M._load_json = load_json

function M.load(game, log)
    log = log or function() end
    game = clean_key(game) or "unknown"
    local notes = {}
    local def = load_json(M.default_path())
    local prof = load_json(M.profile_path(game))
    if not def then
        pcall(log, "profile: " .. M.default_path() .. " missing or broken, using built-in word lists")
    end
    if not prof then
        pcall(log, "profile: " .. M.profile_path(game) .. " missing or broken, guessing from names only")
    end
    local ok, P = pcall(M.merge, def, prof, notes)
    if not ok then
        pcall(log, "profile: merge failed (" .. tostring(P) .. "), using defaults")
        P = M.merge(nil, nil, notes)
    end
    P.game = game
    if #notes > 0 then
        pcall(log, "profile: " .. #notes .. " bad entries dropped (first: " .. notes[1] .. ")")
    end
    return P, { default = def ~= nil, game = prof ~= nil, notes = notes }
end

return M
