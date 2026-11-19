-- sense_re/learn.lua - item state, "taken/opened" learning and door unlock events for the RE Engine Sense helper
-- Read-only towards the game. Loaded with require("sense_re.learn"); M.tick(ctx) runs 10 times a second
-- right after scan.
--
-- Two halves:
--   observe(rec, go, comp, P, entry)  called by scan.lua in the SAME tick it got the objects from findComponents.
--     Reads: GameObject get_Valid (first, always), get_DrawSelf, the profile/learned rules, and - for records within
--     20 m - a snapshot of up to 12 watched bools (bool fields + no-argument get_* getters returning System.Boolean,
--     whose names have a keyword like Open/Get/Taken/Lock). Writes only plain values into rec:
--       rec.gone (bool), rec.why (text), rec.req (bool), rec.unlocked (doors: true/false/nil), rec.st (snapshot|nil)
--   tick(ctx)  works on plain data only (ctx.records from scan): compares snapshots between refreshes, keeps the
--     "near the player" times, learns rules, writes the learned file, builds ctx.items and ctx.door_events.
--
-- Learning: a change counts only while the player is within 4 m of the object (now or in the
-- last 3 s). Changes far from the player never count; for items a far change of a signal even blocks that signal
-- (both directions) for the class for this session. A bool / DrawSelf change must also still hold when the player has
-- walked away (> 6 m), seen in a LATER refresh than the one that first saw the change - flags like "player in range"
-- flip back and are dropped. Vanishing (the object is gone from the next refresh of its type) counts at once.
-- Objects that move (> 1 m between two reads of the same id) teach nothing: no flips that refresh (pending dropped),
-- and no vanish once the id was seen moving or when a new id of the same base turns up within 12 m (scan gives a
-- new id after a 1.5 m jump). A rule is saved after 2 agreeing events on 2 different objects of one class.
-- Learned file: reframework/data/sense_re/learned/<game>.json (json.dump_file / json.load_file):
--   {"v":1,"classes":{cls:{"kind":k,"gone":[RULE],"seen":n,"first":"date"}},"doors":{cls:{"unlocked":RULE,"seen":n}}}
-- Doors: the profile's "unlocked" rule, else the learned one. The first read is only a baseline; a later
-- false -> true is an unlock event (kept 20, newest last) -> ctx.door_events for target.lua.
--
-- Public API:
--   M.init(ctx)  M.tick(ctx)  M.reset()
--   M.observe(rec, go, comp, P, entry)
--   M.evalRule(go, comp, rule, draw?) -> true | false | nil   (nil = could not read; never counts as gone)
--   M.load(game) -> L      M.save(game, L) -> ok      M.sanitize(t) -> L      M.count(L) -> n
--   M.learned() -> L       M.hasPending() -> bool     M.doorEvents() -> list  M.watchList(comp, tname, kind) -> list
--   M.stats() -> table
-- ctx in:  ctx.records, ctx.scan_info, ctx.player {x,y,z}, ctx.now, ctx.profile, ctx.game
-- ctx out: ctx.items = { {id, kind, name, pos={x,y,z}, dist, state={src,tname}} } (not gone, require ok, no doors,
--          within range, nearest first), ctx.door_events, ctx.learned = {count=n}

local C = require("sense_re.core")

local M = {}

M.NEAR_M = 4.0
M.NEAR_MEMORY_S = 3.0
M.LEARN_RADIUS_M = 20       -- snapshots only within this distance (at refresh)
M.AWAY_M = 6.0              -- a pending change is confirmed by a refresh taken while the player is this far away
M.PENDING_MAX_S = 120       -- unconfirmed pending changes are dropped after this
M.MAX_WATCH = 12
M.DOOR_EVENTS_MAX = 20
M.SAVE_GAP_S = 5
M.AGREE_CAP = 2000
M.MOVE_M = 1.0              -- an object that moved more than this between two of its reads does not teach
M.MOVE_NEWID_M = 12         -- old id gone + a new id of the same base (GoName|type) this close = it moved, not a vanish
M.RANGE = 30
M.DIR = "sense_re/learned/"

local sqrt, huge = math.sqrt, math.huge

-- watched bool names (lower case, "get_" / "_" / backing-field marks removed): keyword in, deny word out
M.ITEM_WATCH = { "open", "get", "end", "used", "taken", "take", "pick", "loot", "collect", "interact", "destroy",
    "destory", "activ", "complete", "finish", "gather", "empty", "obtain", "acquire", "broke", "break", "done",
    "setupped", "enable", "exist", "valid", "visible", "disp", "show", "state" }
M.DOOR_WATCH = { "lock", "key", "seal", "open" }
M.DENY_WATCH = { "near", "range", "sight", "render", "hit", "ground", "move", "anim", "camera", "look", "target",
    "focus", "select", "hover", "aim", "debug", "request", "sound", "effect", "gui", "wait", "timer", "blend",
    "pending", "extend", "player", "enemy", "input", "pause", "init", "ready", "update", "dirty", "ragdoll",
    "physics", "legend", "fade" }

local RULE_OK = { draw = true, hidden = true, bool = true, call = true, vanish = true, nameSet = true }
local KINDS = { pickup = true, collectible = true }

---------------------------------------------------------------------------------------------------------------------
-- state
---------------------------------------------------------------------------------------------------------------------
local L = { v = 1, classes = {}, doors = {} }
local GAME = "unknown"
local S

local function newState()
    S = {
        snaps = {},        -- id -> {draw=, b={}, tname=}  (last snapshot, plain)
        lastNear = {},     -- id -> now when the player was within NEAR_M
        pending = {},      -- id -> {tname, kind, pos, sigs = {sig -> {to, rule, t, serial}}}
        agree = {}, agreeN = 0,   -- tname|rulekey -> {ids={}, n, rule, kind}
        farVeto = {},      -- tname|rulekey -> true (items only; a far flip sets both directions of its signal)
        toggleVeto = {},   -- tname|rulekey -> true: that signal flipped back once (a toggle, not "taken") - all kinds
        doorBase = {},     -- id -> {val=bool, rk=rule key}
        doorEvents = {},   -- list, newest last
        prev = {},         -- id -> {search, tname, kind, pos, base, moved} of the last refresh (moved: sticky)
        lastSerial = nil,
        dirty = false, lastSave = -huge,
        watch = {},        -- tname#kind -> list {key, name, how}
        events = 0, learnedNow = 0,
        logged = {},
    }
end
newState()

local function log(msg) pcall(C.log, "learn: " .. msg) end

---------------------------------------------------------------------------------------------------------------------
-- small helpers
---------------------------------------------------------------------------------------------------------------------
local function dist(a, b)
    local dx, dy, dz = a[1] - b[1], a[2] - b[2], a[3] - b[3]
    return sqrt(dx * dx + dy * dy + dz * dz)
end

local function posOf(v)
    if type(v) ~= "table" then return nil end
    if v[1] == nil and v.x == nil and type(v.pos) == "table" then v = v.pos end
    local x, y, z = v[1], v[2], v[3]
    if x == nil then x, y, z = v.x, v.y, v.z end
    x, y, z = C.num(x), C.num(y), C.num(z)
    if x and y and z then return { x, y, z } end
    return nil
end

local function cleanKey(s)
    if type(s) ~= "string" then return nil end
    s = s:lower():gsub("[^%w_%-]", "")
    if s == "" then return nil end
    return s
end

local function today()
    local ok, d = pcall(os.date, "%Y-%m-%d")
    if ok and type(d) == "string" then return d end
    return ""
end

local function ruleKey(r)
    if type(r) ~= "table" then return "?" end
    local t = r.type
    if t == "bool" then return "bool:" .. tostring(r.prop) .. "=" .. tostring(r.is) end
    if t == "call" then return "call:" .. tostring(r.fn) .. "=" .. tostring(r.is) end
    if t == "draw" then return "draw=" .. tostring(r.is) end
    if t == "nameSet" then return "nameSet:" .. tostring(r.prop) end
    return tostring(t)
end
M.ruleKey = ruleKey

local function copyRule(r)
    local o = {}
    for k, v in pairs(r) do o[k] = v end
    return o
end

---------------------------------------------------------------------------------------------------------------------
-- rules (plan RULE types; the UE-only ones - arrayAnyTrue, collision - give nil here)
---------------------------------------------------------------------------------------------------------------------
function M.normalizeRule(r)
    if type(r) ~= "table" or not RULE_OK[r.type] then return nil end
    local t = r.type
    local out = { type = t }
    if t == "bool" then
        if type(r.prop) ~= "string" or r.prop == "" or type(r.is) ~= "boolean" then return nil end
        out.prop, out.is = r.prop, r.is
    elseif t == "call" then
        if type(r.fn) ~= "string" or r.fn == "" then return nil end
        local iv = r.is
        if type(iv) ~= "boolean" and type(iv) ~= "number" and type(iv) ~= "string" then return nil end
        out.fn, out.is = r.fn, iv
    elseif t == "nameSet" then
        if type(r.prop) ~= "string" or r.prop == "" then return nil end
        out.prop = r.prop
    elseif t == "draw" then
        out.is = (r.is == true)
    end
    return out
end

function M.evalRule(go, comp, rule, draw)
    if type(rule) ~= "table" then return nil end
    local t = rule.type
    if t == "draw" or t == "hidden" then
        local d = draw
        if type(d) ~= "boolean" then d = C.pcallm(go, "get_DrawSelf") end
        if type(d) ~= "boolean" then return nil end
        if t == "hidden" then return d == false end
        return d == (rule.is == true)
    elseif t == "vanish" then
        local v = C.pcallm(go, "get_Valid")
        if type(v) ~= "boolean" then return nil end
        return v == false
    elseif t == "bool" then
        if type(rule.prop) ~= "string" then return nil end
        local v = C.pget(comp, rule.prop)
        if type(v) ~= "boolean" then return nil end
        local want = rule.is
        if want == nil then want = true end
        return v == want
    elseif t == "call" then
        if type(rule.fn) ~= "string" then return nil end
        local v = C.pcallm(comp, rule.fn)
        if v == nil then return nil end
        if rule.is == nil then return v == true end
        return v == rule.is
    elseif t == "nameSet" then
        if type(rule.prop) ~= "string" then return nil end
        local v = C.pget(comp, rule.prop)
        if v == nil then return nil end
        local tv = type(v)
        if tv == "string" then return v ~= "" and v ~= "None" end
        if tv == "number" then return v ~= 0 end
        if tv == "boolean" then return v end
        return true
    end
    return nil   -- arrayAnyTrue / collision (UE only) / unknown
end

---------------------------------------------------------------------------------------------------------------------
-- learned file
---------------------------------------------------------------------------------------------------------------------
function M.sanitize(t)
    local out = { v = 1, classes = {}, doors = {} }
    if type(t) ~= "table" then return out end
    if type(t.classes) == "table" then
        for cls, e in pairs(t.classes) do
            if type(cls) == "string" and cls ~= "" and type(e) == "table" then
                local gone = {}
                if type(e.gone) == "table" then
                    for i = 1, #e.gone do
                        local r = M.normalizeRule(e.gone[i])
                        if r then gone[#gone + 1] = r end
                    end
                end
                if #gone > 0 then
                    out.classes[cls] = { kind = KINDS[e.kind] and e.kind or "pickup", gone = gone,
                                         seen = C.num(e.seen) or 0, first = type(e.first) == "string" and e.first or "" }
                end
            end
        end
    end
    if type(t.doors) == "table" then
        for cls, e in pairs(t.doors) do
            if type(cls) == "string" and cls ~= "" and type(e) == "table" then
                local r = M.normalizeRule(e.unlocked)
                if r then out.doors[cls] = { unlocked = r, seen = C.num(e.seen) or 0 } end
            end
        end
    end
    return out
end

function M.path(game) return M.DIR .. (cleanKey(game) or "unknown") .. ".json" end

function M.load(game)
    local j = rawget(_G, "json")
    local raw = nil
    if j and type(j.load_file) == "function" then
        local ok, t = pcall(j.load_file, M.path(game))
        if ok and type(t) == "table" then raw = t end
    end
    local Lx = M.sanitize(raw)
    if raw ~= nil then
        log(string.format("loaded %s: %d learned rule set(s)", M.path(game), M.count(Lx)))
    end
    return Lx
end

function M.save(game, Lx)
    local j = rawget(_G, "json")
    if not (j and type(j.dump_file) == "function") then return false end
    local ok, r = pcall(j.dump_file, M.path(game), Lx or L)
    return ok and r ~= false
end

function M.count(Lx)
    Lx = Lx or L
    local n = 0
    for _ in pairs(type(Lx) == "table" and type(Lx.classes) == "table" and Lx.classes or {}) do n = n + 1 end
    for _ in pairs(type(Lx) == "table" and type(Lx.doors) == "table" and Lx.doors or {}) do n = n + 1 end
    return n
end

function M.learned() return L end
function M.setLearned(Lx) L = M.sanitize(Lx) end     -- tests
function M.doorEvents() return S.doorEvents end
function M.hasPending() return next(S.pending) ~= nil end

---------------------------------------------------------------------------------------------------------------------
-- watched bools (per type, cached; static type metadata only)
---------------------------------------------------------------------------------------------------------------------
local function watchName(n)
    n = n:gsub("k__BackingField", ""):gsub("[<>]", "")
    n = n:lower():gsub("^get_", ""):gsub("^m_", ""):gsub("^_+", "")
    return n
end

local function wanted(n, list)
    for _, d in ipairs(M.DENY_WATCH) do if n:find(d, 1, true) then return false end end
    for _, w in ipairs(list) do if n:find(w, 1, true) then return true end end
    return false
end

function M.watchList(comp, tname, kind)
    local key = tostring(tname) .. "#" .. tostring(kind == "door")
    local c = S.watch[key]
    if c then return c end
    if comp == nil then return {} end
    local list = (kind == "door") and M.DOOR_WATCH or M.ITEM_WATCH
    c = {}
    local seen = {}
    for _, f in ipairs(C.fields(comp)) do
        if #c >= M.MAX_WATCH then break end
        if not f.static and f.type == "System.Boolean" and not seen["f:" .. f.name] and wanted(watchName(f.name), list) then
            seen["f:" .. f.name] = true
            c[#c + 1] = { key = "f:" .. f.name, name = f.name, how = "field" }
        end
    end
    for _, m in ipairs(C.methods(comp)) do
        if #c >= M.MAX_WATCH then break end
        if m.np == 0 and m.ret == "System.Boolean" and type(m.name) == "string" and m.name:find("^get_")
            and not seen["c:" .. m.name] and wanted(watchName(m.name), list) then
            seen["c:" .. m.name] = true
            c[#c + 1] = { key = "c:" .. m.name, name = m.name, how = "call" }
        end
    end
    if C.typeName(comp) ~= nil then S.watch[key] = c end   -- cache only when the type could be read
    return c
end

local function ruleOfSig(sig, v)
    if sig == "draw" then return { type = "draw", is = false } end
    local how, name = sig:sub(1, 1), sig:sub(3)
    if how == "f" then return { type = "bool", prop = name, is = v } end
    return { type = "call", fn = name, is = v }
end

---------------------------------------------------------------------------------------------------------------------
-- observe (scan's tick, objects fresh)
---------------------------------------------------------------------------------------------------------------------
local function doorRule(rec, entry)
    if type(entry) == "table" and type(entry.unlocked) == "table" then return entry.unlocked, "profile" end
    local d = type(L.doors) == "table" and L.doors[rec.tname] or nil
    if type(d) == "table" and type(d.unlocked) == "table" then return d.unlocked, "learned" end
    return nil
end

function M.observe(rec, go, comp, P, entry)
    if type(rec) ~= "table" then return end
    rec.gone, rec.why, rec.req, rec.unlocked, rec.st = false, nil, true, nil, nil
    local valid = C.pcallm(go, "get_Valid")
    if valid == false then
        rec.gone, rec.why = true, "invalid"
        return                                   -- never touch a destroyed object again
    end
    if valid == nil and go ~= nil then
        rec.why = "valid?"
    end
    local draw = C.pcallm(go, "get_DrawSelf")
    if type(draw) ~= "boolean" then draw = nil end
    if rec.kind == "door" then
        local rule, src = doorRule(rec, entry)
        if rule then
            rec.unlocked = M.evalRule(go, comp, rule, draw)
            rec.rule_src, rec.rule_key = src, ruleKey(rule)
        end
    else
        -- require: all must be true (a failed read = not ok)
        if type(entry) == "table" and type(entry.require) == "table" then
            for _, r in ipairs(entry.require) do
                if M.evalRule(go, comp, r, draw) ~= true then rec.req = false; break end
            end
        end
        -- gone: DrawSelf off (default), profile rules, learned rules
        local drawGone = not (type(P) == "table" and P.draw_gone == false)
        if drawGone and draw == false then
            rec.gone, rec.why = true, "draw"
        else
            local lists = {}
            if type(entry) == "table" and type(entry.gone) == "table" then lists[#lists + 1] = { entry.gone, "profile" } end
            local lc = type(L.classes) == "table" and L.classes[rec.tname] or nil
            if type(lc) == "table" and type(lc.gone) == "table" then lists[#lists + 1] = { lc.gone, "learned" } end
            for _, li in ipairs(lists) do
                for _, r in ipairs(li[1]) do
                    if M.evalRule(go, comp, r, draw) == true then
                        rec.gone, rec.why = true, li[2] .. " " .. ruleKey(r)
                        break
                    end
                end
                if rec.gone then break end
            end
        end
    end
    -- snapshot for learning (near records only)
    if rec.dist and rec.dist <= M.LEARN_RADIUS_M then
        local st = { draw = draw, b = {} }
        for _, w in ipairs(M.watchList(comp, rec.tname, rec.kind)) do
            local v
            if w.how == "field" then v = C.pget(comp, w.name) else v = C.pcallm(comp, w.name) end
            if type(v) == "boolean" then st.b[w.key] = v end
        end
        rec.st = st
    end
end

---------------------------------------------------------------------------------------------------------------------
-- learning on plain data
---------------------------------------------------------------------------------------------------------------------
local function markDirty() S.dirty = true end

local function learnRule(tname, kind, rule, n, now)
    if kind == "door" then
        local d = L.doors[tname]
        if d then
            d.seen = math.max(C.num(d.seen) or 0, n)
            return false
        end
        L.doors[tname] = { unlocked = copyRule(rule), seen = n }
        log(string.format("learned door %s: unlocked when %s (%d objects)", tname, ruleKey(rule), n))
    else
        local c = L.classes[tname]
        if not c then
            c = { kind = KINDS[kind] and kind or "pickup", gone = {}, seen = 0, first = today() }
            L.classes[tname] = c
        end
        local rk = ruleKey(rule)
        for _, r in ipairs(c.gone) do
            if ruleKey(r) == rk then c.seen = math.max(c.seen or 0, n); return false end
        end
        c.gone[#c.gone + 1] = copyRule(rule)
        c.seen = math.max(c.seen or 0, n)
        log(string.format("learned %s %s: taken when %s (%d objects)", c.kind, tname, rk, n))
    end
    S.learnedNow = S.learnedNow + 1
    markDirty()
    return true
end

local function confirm(id, tname, kind, rule, now)
    local key = tostring(tname) .. "|" .. ruleKey(rule)
    if kind ~= "door" and S.farVeto[key] then return end
    if S.toggleVeto[key] then return end
    local a = S.agree[key]
    if not a then
        if S.agreeN >= M.AGREE_CAP then S.agree, S.agreeN = {}, 0 end
        a = { ids = {}, n = 0, rule = rule, kind = kind }
        S.agree[key] = a
        S.agreeN = S.agreeN + 1
    end
    if not a.ids[id] then a.ids[id] = true; a.n = a.n + 1 end
    if a.n >= 2 then learnRule(tname, kind, rule, a.n, now) end
end

local function isNear(id, d, now)
    if d and d <= M.NEAR_M then return true end
    local t = S.lastNear[id]
    return t ~= nil and now - t <= M.NEAR_MEMORY_S and now >= t
end

-- block a signal for the class, both directions (a flag seen flipping one way may be the idle value the other way)
local function vetoBoth(tbl, tname, sig)
    local base = tostring(tname) .. "|"
    tbl[base .. ruleKey(ruleOfSig(sig, true))] = true
    tbl[base .. ruleKey(ruleOfSig(sig, false))] = true
end

-- a signal that flips back is a toggle (e.g. "player in range"): block it for the class, both directions
local function vetoToggle(tname, sig) vetoBoth(S.toggleVeto, tname, sig) end

-- serial b is a later refresh than a (serials are numbers that go up; anything else: just "different")
local function later(b, a)
    if type(a) == "number" and type(b) == "number" then return b > a end
    return b ~= a
end

local function flip(rec, sig, to, now, serial)
    S.events = S.events + 1
    local rule = ruleOfSig(sig, to)
    local p0 = S.pending[rec.id]
    if p0 and p0.sigs[sig] and p0.sigs[sig].to ~= to then
        p0.sigs[sig] = nil                          -- flipped back before it was confirmed
        if next(p0.sigs) == nil then S.pending[rec.id] = nil end
        vetoToggle(rec.tname, sig)
        return
    end
    if S.toggleVeto[tostring(rec.tname) .. "|" .. ruleKey(rule)] then return end
    if isNear(rec.id, rec.dist, now) then
        local p = S.pending[rec.id]
        if not p then
            p = { tname = rec.tname, kind = rec.kind, pos = rec.pos, sigs = {} }
            S.pending[rec.id] = p
        end
        p.sigs[sig] = { to = to, rule = rule, t = now, serial = serial }
    elseif rec.kind ~= "door" then
        -- far flip: block this signal for the class in BOTH directions (doors: a far unlock is normal, no veto)
        vetoBoth(S.farVeto, rec.tname, sig)
    end
end

local function baseOf(rec) return tostring(rec.go_name or "") .. "|" .. tostring(rec.tname) end

-- a vanished id whose object really moved: a new id of the same base showed up close to its last place
local function movedToNewId(p, newByBase)
    local l = p.base and newByBase[p.base]
    if not (l and p.pos) then return false end
    for _, pos in ipairs(l) do
        if dist(p.pos, pos) <= M.MOVE_NEWID_M then return true end
    end
    return false
end

local function pushDoorEvent(rec, now)
    local evs = S.doorEvents
    for i = #evs, 1, -1 do if evs[i].id == rec.id then table.remove(evs, i) end end
    evs[#evs + 1] = { id = rec.id, pos = { rec.pos[1], rec.pos[2], rec.pos[3] }, t = now, kind = "door",
                      label = rec.name or "", cls = rec.tname }
    while #evs > M.DOOR_EVENTS_MAX do table.remove(evs, 1) end
    log(string.format("door unlocked: %s (%s)", tostring(rec.id), tostring(rec.rule_src)))
end

-- one refresh of scan arrived: compare with the last one
local function onRefresh(recs, info, ppos, now)
    local cur = {}
    for _, rec in ipairs(recs) do if rec.id then cur[rec.id] = rec end end
    -- vanished: in the last refresh, its search type was read completely now, and it is not there any more.
    -- Not counted when the id was seen moving (> MOVE_M between reads), or when a new id of the same base turned up
    -- close by (scan hands out a new id when an object jumps > 1.5 m: a part riding on a character, not a pickup).
    if not info.area_changed then
        local refreshed = type(info.refreshed) == "table" and info.refreshed or {}
        local newByBase = nil
        for id, p in pairs(S.prev) do
            if not cur[id] and p.search and refreshed[p.search] and p.kind ~= "door" and not p.moved then
                if newByBase == nil then
                    newByBase = {}
                    for _, rec in ipairs(recs) do
                        if rec.id and not S.prev[rec.id] and type(rec.pos) == "table" then
                            local b = baseOf(rec)
                            local l = newByBase[b]
                            if not l then l = {}; newByBase[b] = l end
                            l[#l + 1] = rec.pos
                        end
                    end
                end
                local d = ppos and p.pos and dist(p.pos, ppos) or nil
                if not movedToNewId(p, newByBase) and isNear(id, d, now) then
                    S.events = S.events + 1
                    confirm(id, p.tname, p.kind, { type = "vanish" }, now)
                end
            end
        end
    end
    -- forget state of objects that left
    for id in pairs(S.prev) do
        if not cur[id] then
            S.snaps[id], S.pending[id], S.doorBase[id], S.lastNear[id] = nil, nil, nil, nil
        end
    end
    local prev = {}
    for _, rec in ipairs(recs) do
        if rec.id then
            -- moved: compared with where this id was at its last read (S.prev keeps the last read position)
            local pp = S.prev[rec.id]
            local movedNow = false
            if rec.obs == info.serial and pp and type(pp.pos) == "table" and type(rec.pos) == "table" then
                movedNow = dist(pp.pos, rec.pos) > M.MOVE_M
            end
            prev[rec.id] = { search = rec.search, tname = rec.tname, kind = rec.kind, pos = rec.pos, base = baseOf(rec),
                             moved = movedNow or (pp ~= nil and pp.moved == true) }
            if rec.obs == info.serial then
                local id = rec.id
                -- a moving item teaches nothing this refresh (a part riding on a character); its pending goes too.
                -- Doors keep learning: a sliding door moves when it opens, right after its lock flips.
                local skip = movedNow and rec.kind ~= "door"
                if skip then S.pending[id] = nil end
                -- snapshot compare
                local st = rec.st
                local old = S.snaps[id]
                if st and old and old.tname == rec.tname and not skip then
                    if old.draw == true and st.draw == false then flip(rec, "draw", false, now, info.serial) end
                    for k, v in pairs(st.b) do
                        local pv = old.b[k]
                        if pv ~= nil and pv ~= v then flip(rec, k, v, now, info.serial) end
                    end
                end
                if st then
                    S.snaps[id] = { draw = st.draw, b = st.b, tname = rec.tname }
                else
                    S.snaps[id] = nil
                end
                -- pending changes: dropped when they flipped back, confirmed when they still hold with the player away
                -- in a LATER refresh than the one that first saw them (never in the same one)
                local p = S.pending[id]
                if p then
                    for sig, ps in pairs(p.sigs) do
                        local v = nil
                        if st then if sig == "draw" then v = st.draw else v = st.b[sig] end end
                        if st == nil or now - ps.t > M.PENDING_MAX_S then
                            p.sigs[sig] = nil
                        elseif v ~= nil and v ~= ps.to then
                            p.sigs[sig] = nil
                            vetoToggle(p.tname, sig)
                        elseif v == ps.to and rec.dist and rec.dist > M.AWAY_M and later(info.serial, ps.serial) then
                            p.sigs[sig] = nil
                            confirm(id, p.tname, p.kind, ps.rule, now)
                        end
                    end
                    if next(p.sigs) == nil then S.pending[id] = nil end
                end
                -- doors: unlock events after a baseline read
                if rec.kind == "door" and rec.unlocked ~= nil then
                    local rk = tostring(rec.rule_src) .. ":" .. tostring(rec.rule_key)
                    local b = S.doorBase[id]
                    if b and b.rk == rk and b.val == false and rec.unlocked == true then pushDoorEvent(rec, now) end
                    S.doorBase[id] = { val = rec.unlocked, rk = rk }
                end
            end
        end
    end
    S.prev = prev
end

function M.tick(ctx)
    if type(ctx) ~= "table" then return end
    local now = C.num(ctx.now) or C.clock()
    local P = type(ctx.profile) == "table" and ctx.profile or {}
    local recs = type(ctx.records) == "table" and ctx.records or {}
    local info = type(ctx.scan_info) == "table" and ctx.scan_info or nil
    local ppos = posOf(ctx.player)
    -- near times (every tick, plain positions)
    if ppos then
        for _, rec in ipairs(recs) do
            if rec.id and rec.pos then
                local d = dist(rec.pos, ppos)
                rec.dist = d
                if d <= M.NEAR_M then S.lastNear[rec.id] = now end
            end
        end
    end
    -- a new refresh from scan
    if info and info.changed and info.serial ~= S.lastSerial then
        S.lastSerial = info.serial
        onRefresh(recs, info, ppos, now)
    end
    -- old pending entries
    for id, p in pairs(S.pending) do
        for sig, ps in pairs(p.sigs) do
            if now - ps.t > M.PENDING_MAX_S or now < ps.t then p.sigs[sig] = nil end
        end
        if next(p.sigs) == nil then S.pending[id] = nil end
    end
    -- items for out.lua
    local range = C.num(P.range) or M.RANGE
    local items = {}
    if ppos then
        for _, rec in ipairs(recs) do
            if rec.kind ~= "door" and not rec.gone and rec.req ~= false and rec.pos and rec.dist and rec.dist <= range then
                items[#items + 1] = { id = rec.id, kind = rec.kind, name = rec.name or "", pos = rec.pos,
                                      dist = rec.dist, state = { src = rec.src, tname = rec.tname } }
            end
        end
        table.sort(items, function(a, b) return a.dist < b.dist end)
    end
    ctx.items = items
    ctx.door_events = S.doorEvents
    ctx.learned = { count = M.count(L) }
    -- save (rare: only after something was learned)
    if S.dirty and now - S.lastSave >= M.SAVE_GAP_S then
        S.lastSave = now
        if M.save(GAME, L) then
            S.dirty = false
        elseif not S.logged.savefail then
            S.logged.savefail = true
            log("could not write " .. M.path(GAME) .. " (will retry)")
        end
    end
    return items
end

function M.init(ctx)
    newState()
    local g = type(ctx) == "table" and (ctx.game or (type(ctx.profile) == "table" and ctx.profile.game)) or nil
    GAME = cleanKey(g) or "unknown"
    L = M.load(GAME)
end

function M.reset()
    newState()
end

function M.stats()
    local np = 0
    for _ in pairs(S.pending) do np = np + 1 end
    return { events = S.events, learned_now = S.learnedNow, pending = np, door_events = #S.doorEvents,
             count = M.count(L), dirty = S.dirty, game = GAME }
end

return M
