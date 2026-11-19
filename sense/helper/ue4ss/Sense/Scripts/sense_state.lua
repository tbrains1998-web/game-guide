-- sense_state.lua: "is this item taken / opened?" and "learn how a new class shows that". READ-ONLY.
-- Loaded with require("sense_state"); returns a table of functions. Nothing here is global and nothing is written
-- to the game or to files (the caller saves the learned table L with sense_profile / sense_files).
--
-- Every game read goes through sense_ue (ctx.ue, or the module set with M.setUe, or require("sense_ue")):
-- readBool / readNum / readName / readStr / readHidden / arrayAnyTrue / callCls / actorSignals / watchProps /
-- readVec / call / valid. Those are all pcall-guarded there. The few reads done here (a named position property of
-- a door) are inside pcall too. The caller runs all of this on the game thread with its in-flight guard.
--
-- RULE (profile and learned file):
--   {type="hidden"}                       actor hidden (bHidden, else IsHidden())            ("is" optional, default true)
--   {type="bool", prop=P, is=true|false}  a BoolProperty equals is (only a real boolean counts)
--   {type="arrayAnyTrue", prop=P, max=64} array has a true entry. Entries are never read (that path crashed PBZ), so
--                                         this is false for an empty array and unknown (nil) otherwise, like main.lua
--   {type="call", fn=F, is=v}             a no-argument UFunction returns v (per-class "missing function" memory)
--   {type="nameSet", prop=P}              a Name/String property is set (not "", not "None")
--   {type="vanish"}                       the object is no longer valid
--   {type="collision", is=false}          GetActorEnableCollision() equals is
--   {type="num", prop=P, is=n}            a number (Byte/Enum "*State*") property equals n (only learned rules use it)
--   {type="draw", ...}                    RE Engine only: always unknown here
-- evalRule gives true / false / nil. nil = could not be read = never counts (a failed read never hides an item).

local M = {}

M.VERSION        = "sense_state/1"
M.NEAR_M         = 4.0    -- a change counts for learning only while the player is this close (metres) ...
M.NEAR_S         = 3.0    -- ... or was this close within the last NEAR_S seconds
M.LEARN_OBJECTS  = 2      -- agreeing changes on this many DIFFERENT objects of one class -> learned rule
M.MAX_WATCH      = 16     -- most watched properties read per snapshot
M.MAX_PENDING    = 64     -- most pending rule keys kept per class (session only)
M.MAX_REQ_CACHE  = 20000  -- most cached "require" answers (static level data, read once per object)
M.MAX_DOORS      = 20000  -- most door baselines kept
M.MAX_BASE       = 32     -- most first-seen property values kept per item (it._base)
M.MAX_BASE_IDS   = 20000  -- most objects whose first-seen values are kept per learned table (session only; x2 with
                          -- the older half kept while the newer one fills)
M.PICKUP_RULE    = { type = "call", fn = "IsValidPickup", is = false }   -- main.lua: every "pickup" gets this check

-- the last internal error (a bug here must never stop the tick; every public function catches its own errors)
M.lastError = nil

-- ---------- sense_ue access ----------
local UE = nil
do
    local ok, m = pcall(require, "sense_ue")
    if ok and type(m) == "table" then UE = m end
end

function M.setUe(ue) UE = ue end

local function ueOf(ctx)
    if type(ctx) == "table" and type(ctx.ue) == "table" then return ctx.ue end
    return UE
end

-- ---------- small pure helpers ----------
local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

local function today()
    local ok, s = pcall(os.date, "%Y-%m-%d")
    if ok and type(s) == "string" then return s end
    return "?"
end
M.today = today

local function fail(where, err)
    M.lastError = where .. ": " .. tostring(err)
end

-- the rule's "is" with its default (hidden: true, call: false only when given, others: true)
local function isOf(rule, default)
    local v = rule.is
    if v == nil then return default end
    return v
end

-- a stable text key for a rule: "bool:CurSwitchState:true", "hidden::true", "vanish::"
function M.ruleKey(rule)
    if type(rule) ~= "table" then return "?" end
    local t = tostring(rule.type)
    local what = rule.prop or rule.fn or ""
    local is = rule.is
    if t == "hidden" and is == nil then is = true end
    if t == "collision" and is == nil then is = false end
    if t == "vanish" or t == "arrayAnyTrue" or t == "nameSet" then is = nil end
    return t .. ":" .. tostring(what) .. ":" .. (is == nil and "" or tostring(is))
end
local ruleKey = M.ruleKey

local function copyRule(r)
    local c = {}
    for k, v in pairs(r) do c[k] = v end
    return c
end

-- ---------- RULE evaluator ----------
-- returns result (true|false|nil) and the raw value read (for the debug "state" column; may be nil)
local function eval(obj, rule, ctx, cls)
    if type(rule) ~= "table" then return nil, nil end
    local ue = ueOf(ctx)
    if not ue then return nil, nil end
    local t = rule.type
    if t == "vanish" then
        local v = ue.valid(obj)
        return v ~= true, v
    end
    if obj == nil then return nil, nil end
    if t == "hidden" then
        local h = ue.readHidden(obj)
        if type(h) ~= "boolean" then return nil, nil end
        return h == isOf(rule, true), h
    elseif t == "bool" then
        if type(rule.prop) ~= "string" then return nil, nil end
        local v = ue.readBool(obj, rule.prop)
        if type(v) ~= "boolean" then return nil, nil end
        local want = isOf(rule, true)
        if type(want) ~= "boolean" then return nil, v end
        return v == want, v
    elseif t == "num" then
        if type(rule.prop) ~= "string" or not finite(rule.is) then return nil, nil end
        local v = ue.readNum(obj, rule.prop)
        if not finite(v) then return nil, nil end
        return v == rule.is, v
    elseif t == "arrayAnyTrue" then
        if type(rule.prop) ~= "string" then return nil, nil end
        local max = finite(rule.max) and rule.max or 64
        if max > 64 then max = 64 end
        local r, n = ue.arrayAnyTrue(obj, rule.prop, max)
        if r ~= true and r ~= false then r = nil end
        return r, n
    elseif t == "call" then
        if type(rule.fn) ~= "string" then return nil, nil end
        local v
        if ue.callCls and type(cls) == "string" then
            v = ue.callCls(obj, cls, rule.fn)
        else
            v = ue.call(obj, rule.fn)
        end
        local want = rule.is
        if v == nil or want == nil or type(v) ~= type(want) then return nil, v end
        return v == want, v
    elseif t == "nameSet" then
        if type(rule.prop) ~= "string" then return nil, nil end
        local s = ue.readName(obj, rule.prop)
        if type(s) ~= "string" then return nil, nil end
        return s ~= "" and s ~= "None", s
    elseif t == "collision" then
        local v
        if ue.callCls and type(cls) == "string" then
            v = ue.callCls(obj, cls, "GetActorEnableCollision")
        else
            v = ue.call(obj, "GetActorEnableCollision")
        end
        if type(v) ~= "boolean" then return nil, nil end
        local want = isOf(rule, false)
        if type(want) ~= "boolean" then return nil, v end
        return v == want, v
    end
    return nil, nil   -- "draw" (RE only) and unknown types
end

-- evalRule(obj, rule, ctx) -> true | false | nil, raw value.  ctx (optional): {ue=sense_ue, cls="class name"}.
-- With ctx.cls a "call" rule uses sense_ue's per-class "missing function" memory.
function M.evalRule(obj, rule, ctx)
    local ok, r, raw = pcall(eval, obj, rule, ctx, type(ctx) == "table" and ctx.cls or nil)
    if not ok then fail("evalRule", r); return nil, nil end
    return r, raw
end

-- ---------- which rules apply to an item ----------
local function listOf(x)
    if type(x) ~= "table" then return nil end
    return x
end

-- the learned class entry of L (or nil)
local function learnedClass(L, cls)
    if type(L) ~= "table" or type(L.classes) ~= "table" or type(cls) ~= "string" then return nil end
    local c = L.classes[cls]
    if type(c) == "table" then return c end
    return nil
end

local function learnedDoor(L, cls)
    if type(L) ~= "table" or type(L.doors) ~= "table" or type(cls) ~= "string" then return nil end
    local d = L.doors[cls]
    if type(d) == "table" then return d end
    return nil
end

-- is the entry a profile seed (sense_profile marks seeds and profile doors with seed = true)? Then its own rules
-- are the whole truth: learned rules are not used for it (PBZ stays exactly as the profile says) and nothing is
-- learned for it. Guessed entries (guess_gone) and learned entries are not seeds.
local function isSeed(entry, L, cls)
    if type(entry) ~= "table" or entry.seed ~= true then return false end
    return entry ~= learnedClass(L, cls) and entry ~= learnedDoor(L, cls)
end

-- the full gone-rule list of an item: vanish + hidden (main.lua: every item) + the entry's own rules + the learned
-- rules of its class from ctx.L (not for profile seeds) + IsValidPickup for "pickup" items (main.lua). No duplicates.
-- entry.implicit == false switches the implicit vanish/hidden/IsValidPickup rules off.
function M.goneRules(it, entry, ctx)
    local out, seen = {}, {}
    local function add(r)
        if type(r) ~= "table" or type(r.type) ~= "string" then return end
        local k = ruleKey(r)
        if seen[k] then return end
        seen[k] = true
        out[#out + 1] = r
    end
    local implicit = not (type(entry) == "table" and entry.implicit == false)
    if implicit then add({ type = "vanish" }); add({ type = "hidden" }) end
    local L = type(ctx) == "table" and ctx.L or nil
    local cls = type(it) == "table" and it.cls or nil
    local own = type(entry) == "table" and listOf(entry.gone) or nil
    if own and #own > 0 then
        for _, r in ipairs(own) do add(r) end
    end
    if not isSeed(entry, L, cls) then
        local lc = learnedClass(L, cls)
        if lc and listOf(lc.gone) then for _, r in ipairs(lc.gone) do add(r) end end
    end
    local kind = (type(it) == "table" and it.kind) or (type(entry) == "table" and entry.kind) or nil
    local pr = type(ctx) == "table" and ctx.pickupRule
    if pr == nil then pr = M.PICKUP_RULE end
    if implicit and kind == "pickup" and pr then add(pr) end
    return out
end

local function goneImpl(it, entry, ctx, onlyFast)
    if type(it) ~= "table" then return false, {} end
    local obj = it.obj
    local cls = it.cls
    local detail = {}
    local rules = M.goneRules(it, entry, ctx)
    -- vanish first: an object that is no longer valid is gone, and nothing else is read from it
    for _, r in ipairs(rules) do
        if r.type == "vanish" then
            local g = eval(obj, r, ctx, cls)
            detail[ruleKey(r)] = g
            if g == true then return true, detail end
        end
    end
    local gone = false
    for _, r in ipairs(rules) do
        if r.type ~= "vanish" and (not onlyFast or r.type == "call") then
            local res, raw = eval(obj, r, ctx, cls)
            detail[ruleKey(r)] = raw
            if res == true then gone = true end
        end
    end
    return gone, detail
end

-- isGone(it, entry, ctx) -> gone (bool), detail {ruleKey -> raw value read}.
-- it = {obj=, cls=, kind=, id=}; entry = profile seed / learned / guessed entry (its "gone" list); ctx = {ue=, L=}.
-- gone = any rule is true. A rule that could not be read (nil) never counts. Every rule is read (for the debug
-- "state" column), except when the object is invalid (then only that).
function M.isGone(it, entry, ctx)
    local ok, g, d = pcall(goneImpl, it, entry, ctx, false)
    if not ok then fail("isGone", g); return false, {} end
    return g == true, d
end

-- fastGone: only the cheap per-tick checks of main.lua (object no longer valid, "call" rules such as
-- IsValidPickup). Same results as isGone for those rules.
function M.fastGone(it, entry, ctx)
    local ok, g, d = pcall(goneImpl, it, entry, ctx, true)
    if not ok then fail("fastGone", g); return false, {} end
    return g == true, d
end

-- ---------- require (all must pass to count as an item at all) ----------
local reqCache, reqCacheN = {}, 0

local function allNameSet(rules)
    for _, r in ipairs(rules) do
        if type(r) ~= "table" or r.type ~= "nameSet" then return false end
    end
    return true
end

local function requireImpl(obj, entry, ctx, id)
    if type(entry) ~= "table" then return true end
    local rules = listOf(entry.require)
    if not rules or #rules == 0 then return true end
    -- nameSet-only requirements are level data (LootItemID): read once per object id, like main.lua's hasLoot.
    -- Only a definite answer is remembered; a failed read is tried again next time.
    local cacheable = type(id) == "string" and allNameSet(rules)
    local cache = (type(ctx) == "table" and type(ctx.reqCache) == "table") and ctx.reqCache or nil
    if cacheable then
        local known
        if cache then known = cache[id] else known = reqCache[id] end
        if known ~= nil then return known end
    end
    local cls = type(ctx) == "table" and ctx.cls or nil
    local result = true
    local unknown = false
    for _, r in ipairs(rules) do
        local v = eval(obj, r, ctx, cls)
        if v == nil then unknown = true; result = false
        elseif v == false then result = false; unknown = false; break end
    end
    if cacheable and not unknown then
        if cache then
            cache[id] = result
        else
            if reqCacheN >= M.MAX_REQ_CACHE then reqCache, reqCacheN = {}, 0 end
            if reqCache[id] == nil then reqCacheN = reqCacheN + 1 end
            reqCache[id] = result
        end
    end
    return result
end

-- requireOk(obj, entry, ctx?, id?) -> true when every entry.require rule is true. A rule that cannot be read
-- counts as NOT ok (main.lua: a loot spot whose LootItemID cannot be read is not an item yet).
function M.requireOk(obj, entry, ctx, id)
    local ok, r = pcall(requireImpl, obj, entry, ctx, id)
    if not ok then fail("requireOk", r); return false end
    return r == true
end

function M.resetRequireCache() reqCache, reqCacheN = {}, 0 end

-- ---------- snapshots of watched values ----------
-- remember the first value seen for each watched property of an object (at most MAX_BASE names per object).
-- the values live in a per-session store keyed by the learned table L and the object id (like pendingByL),
-- NOT only on the item table: main.lua makes a new item/door table whenever one item-scan answer leaves the object
-- out (levels streaming), and a first-seen value lost that way let "back to idle" learn a wrong rule.
-- it._baseRec = {vals = {name -> value}, n = count}; it._base = it._baseRec.vals (what learnImpl reads).
-- An item that is not linked yet (no L / no id) keeps a private record; linking merges it into the session one
-- (the session value wins: it was seen first).
-- baseByL[L] = {cur = {id -> rec}, old = {id -> rec}, n = #cur}; bounded: when cur holds MAX_BASE_IDS records it
-- becomes old and a new cur starts (an id found only in old moves back to cur). Weak by L, so a new L starts fresh.
local baseByL = setmetatable({}, { __mode = "k" })

local function baseRecOf(it)
    local rec = it._baseRec
    if type(rec) ~= "table" or type(rec.vals) ~= "table" then
        rec = { vals = type(it._base) == "table" and it._base or {}, n = 0 }
        for _ in pairs(rec.vals) do rec.n = rec.n + 1 end
        it._baseRec = rec
    end
    it._base = rec.vals
    return rec
end

local function sessionBase(L, key)
    local s = baseByL[L]
    if type(s) ~= "table" then s = { cur = {}, old = {}, n = 0 }; baseByL[L] = s end
    local rec = s.cur[key]
    if rec then return rec end
    rec = s.old[key]
    if (s.n or 0) >= M.MAX_BASE_IDS then s.old, s.cur, s.n = s.cur, {}, 0 end
    if not rec then rec = { vals = {}, n = 0, linked = true } end
    s.cur[key] = rec
    s.n = (s.n or 0) + 1
    return rec
end

-- link the item to the session record of (L, it.id); safe to call often (no-op once linked to the same record)
local function linkBase(it, L)
    if type(it) ~= "table" or type(L) ~= "table" then return end
    local id = it.id
    if id == nil then return end
    local own = baseRecOf(it)
    local rec = sessionBase(L, tostring(id))
    if own == rec then return end
    -- a private record (first seen before linking) is merged; a session record from before resetBase (map change)
    -- is dropped, so a reset really forgets it
    if own.linked then own = { vals = {} } end
    for name, v in pairs(own.vals) do
        if rec.vals[name] == nil and rec.n < M.MAX_BASE then
            rec.vals[name] = v
            rec.n = rec.n + 1
        end
    end
    it._baseRec = rec
    it._base = rec.vals
end

local function rememberBase(it, sn)
    if type(it) ~= "table" then return end
    local rec = baseRecOf(it)
    local base = rec.vals
    if type(sn) ~= "table" or sn.valid ~= true or type(sn.props) ~= "table" then return end
    for name, v in pairs(sn.props) do
        if type(name) == "string" and base[name] == nil and (type(v) == "boolean" or finite(v)) then
            local n = finite(rec.n) and rec.n or 0
            if n >= M.MAX_BASE then return end
            base[name] = v
            rec.n = n + 1
        end
    end
end

-- resetBase(L?) -> forget the first-seen values of L (every L when nil). main.lua calls it on a map change.
-- Items still cached keep their current record until they are linked again.
function M.resetBase(L)
    if L == nil then
        for k in pairs(baseByL) do baseByL[k] = nil end
    elseif type(L) == "table" then
        baseByL[L] = nil
    end
end
-- how many objects have a session first-seen record for L (tests / log)
function M.baseCount(L)
    local s = baseByL[L]
    if type(s) ~= "table" then return 0 end
    local n = 0
    for _ in pairs(s.cur) do n = n + 1 end
    for k in pairs(s.old) do if s.cur[k] == nil then n = n + 1 end end
    return n
end

-- snapshot(it, watch?, ctx?) -> {valid=, hidden=, collision=, props={name -> boolean|number}}
-- Also records the item's first-seen property values (it._base) for learning.
-- watch = list of property names (sense_ue.watchProps names), or {names=, types=}; nil = ask sense_ue.watchProps.
-- types (optional) {name -> "bool"|"num"}. An invalid object gives {valid=false, props={}} and nothing else is read.
local function snapImpl(it, watch, ctx)
    local ue = ueOf(ctx)
    if type(it) ~= "table" or not ue then return nil end
    local obj, cls = it.obj, it.cls
    local sig = ue.actorSignals(obj, cls)
    if type(sig) ~= "table" then sig = { valid = ue.valid(obj) } end
    local snap = { valid = sig.valid == true, props = {} }
    if not snap.valid then return snap end
    if type(sig.hidden) == "boolean" then snap.hidden = sig.hidden end
    if type(sig.collision) == "boolean" then snap.collision = sig.collision end
    local names, types = nil, nil
    if type(watch) == "table" and type(watch.names) == "table" then
        names, types = watch.names, watch.types
    elseif type(watch) == "table" then
        names = watch
    else
        local okw, n, ty = pcall(ue.watchProps, obj, type(ctx) == "table" and ctx.words or nil, cls)
        if okw then names, types = n, ty end
    end
    if type(names) ~= "table" then return snap end
    if type(types) ~= "table" then types = {} end
    local count = 0
    for _, n in ipairs(names) do
        if count >= M.MAX_WATCH then break end
        local name, ty = n, types[n]
        if type(n) == "table" then name, ty = n.name or n[1], n.type or n[2] end
        if type(name) == "string" and name ~= "" then
            count = count + 1
            local v = nil
            if ty == "bool" or ty == "BoolProperty" then
                v = ue.readBool(obj, name)
            elseif ty == "num" or ty == "ByteProperty" or ty == "EnumProperty" then
                v = ue.readNum(obj, name)
            else
                v = ue.readBool(obj, name)
                if v == nil then v = ue.readNum(obj, name) end
            end
            if type(v) == "boolean" or finite(v) then snap.props[name] = v end
        end
    end
    return snap
end

function M.snapshot(it, watch, ctx)
    local ok, s = pcall(snapImpl, it, watch, ctx)
    if not ok then fail("snapshot", s); return nil end
    if type(s) == "table" then
        -- ctx.L (optional): link the item to its per-session first-seen record before recording
        if type(ctx) == "table" and type(ctx.L) == "table" then
            local okl, e = pcall(linkBase, it, ctx.L)
            if not okl then fail("snapshot", e) end
        end
        local okb, e = pcall(rememberBase, it, s)
        if not okb then fail("snapshot", e) end
    end
    return s
end

-- the changes between two snapshots, as candidate rules, strongest first:
-- vanish, hidden (false->true), bool flips (by name), number changes (by name), collision (true->false).
-- Also returns the "back" changes (hidden true->false, collision false->true) as a set of rule keys whose vote of
-- this object must be taken back. Only values read in BOTH snapshots count (a failed read is never a change).
function M.diff(prev, now)
    local changes = {}
    if type(prev) ~= "table" or type(now) ~= "table" then return changes end
    if prev.valid == true and now.valid == false then
        changes[1] = { type = "vanish" }
        return changes
    end
    if prev.valid ~= true or now.valid ~= true then return changes end
    if prev.hidden == false and now.hidden == true then changes[#changes + 1] = { type = "hidden" } end
    local pp, np = type(prev.props) == "table" and prev.props or {}, type(now.props) == "table" and now.props or {}
    local names = {}
    for name in pairs(np) do if type(name) == "string" then names[#names + 1] = name end end
    table.sort(names)
    local nums = {}
    for _, name in ipairs(names) do
        local a, b = pp[name], np[name]
        if type(a) == "boolean" and type(b) == "boolean" and a ~= b then
            changes[#changes + 1] = { type = "bool", prop = name, is = b }
        elseif finite(a) and finite(b) and a ~= b then
            nums[#nums + 1] = { type = "num", prop = name, is = b }
        end
    end
    for _, r in ipairs(nums) do changes[#changes + 1] = r end
    if prev.collision == true and now.collision == false then changes[#changes + 1] = { type = "collision", is = false } end
    return changes
end

-- the opposite changes (a value going back): hidden true->false, collision false->true
local function backKeys(prev, now)
    local keys = {}
    if type(prev) ~= "table" or type(now) ~= "table" or prev.valid ~= true or now.valid ~= true then return keys end
    if prev.hidden == true and now.hidden == false then keys[#keys + 1] = ruleKey({ type = "hidden" }) end
    if prev.collision == false and now.collision == true then keys[#keys + 1] = ruleKey({ type = "collision", is = false }) end
    return keys
end

-- ---------- nearness (player within NEAR_M now, or within the last NEAR_S seconds) ----------
-- nearNow(it, dist, now) -> bool. Remembers on the item when the player was last near (it._nearAt).
function M.nearNow(it, dist, now)
    if type(it) ~= "table" or not finite(now) then return false end
    if finite(dist) and dist <= M.NEAR_M then
        it._nearAt = now
        return true
    end
    local at = it._nearAt
    return finite(at) and now - at >= 0 and now - at <= M.NEAR_S
end

-- ---------- learning bookkeeping (session only, never saved) ----------
-- pending[L] = {cls -> {ruleKey -> {rule=, ids={id->true}, n=, events=}}}; weak by L, so a new L starts fresh
local pendingByL = setmetatable({}, { __mode = "k" })

local function pendingOf(L)
    local p = pendingByL[L]
    if not p then p = {}; pendingByL[L] = p end
    return p
end
function M.pending(L) return pendingByL[L] end

local function knownRule(L, cls, isDoor, key)
    if isDoor then
        local d = learnedDoor(L, cls)
        return d ~= nil and type(d.unlocked) == "table" and ruleKey(d.unlocked) == key
    end
    local c = learnedClass(L, cls)
    if not c or not listOf(c.gone) then return false end
    for _, r in ipairs(c.gone) do if ruleKey(r) == key then return true end end
    return false
end

local function countKeys(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

-- take back this object's vote for a rule key (the value went back, so it was not a lasting "taken" change)
local function unvote(pc, key, id)
    local rec = pc[key]
    if rec and rec.ids[id] then
        rec.ids[id] = nil
        rec.n = rec.n - 1
        if rec.n <= 0 then pc[key] = nil end
        return true
    end
    return false
end

-- write a learned rule into L. Items: L.classes[cls] = {kind, gone=[RULE], seen, first}; doors: L.doors[cls] =
-- {unlocked=RULE, seen}. Returns true when L changed.
local function writeLearned(L, cls, kind, isDoor, rule, events)
    if isDoor then
        -- "vanish" is never a door's "unlocked" rule: a vanished door has no position, so it could never give a
        -- target, and the class would be stuck with it (an old learned "vanish" rule may be replaced)
        if rule.type == "vanish" then return false end
        if type(L.doors) ~= "table" then L.doors = {} end
        local d = L.doors[cls]
        if type(d) == "table" and type(d.unlocked) == "table" and d.unlocked.type ~= "vanish" then return false end
        L.doors[cls] = { unlocked = copyRule(rule), seen = events, first = M.today() }
        return true
    end
    if type(L.classes) ~= "table" then L.classes = {} end
    local c = L.classes[cls]
    kind = (kind == "collectible") and "collectible" or "pickup"     -- sense_profile only keeps these two kinds
    if type(c) ~= "table" then
        c = { kind = kind, gone = {}, seen = 0, first = M.today() }
        L.classes[cls] = c
    end
    if type(c.gone) ~= "table" then c.gone = {} end
    local key = ruleKey(rule)
    for _, r in ipairs(c.gone) do if ruleKey(r) == key then return false end end
    c.gone[#c.gone + 1] = copyRule(rule)
    c.seen = (finite(c.seen) and c.seen or 0) + events
    if c.kind ~= "pickup" and c.kind ~= "collectible" then c.kind = kind end
    return true
end

local function learnImpl(it, snapPrev, snapNow, playerNear, now, L)
    if type(it) ~= "table" or type(L) ~= "table" then return nil end
    local cls = it.cls
    if type(cls) ~= "string" or cls == "" then return nil end
    local id = it.id
    if id == nil then return nil end
    id = tostring(id)
    local near
    if type(playerNear) == "boolean" then near = playerNear
    else near = M.nearNow(it, playerNear, now) end
    local isDoor = it.kind == "door"
    local pcls = pendingOf(L)[cls]
    -- the first value seen for each watched property of this object (kept per session by object id, so it
    -- survives the caller rebuilding the item/door table): a change back to it is the idle state, never "taken"
    linkBase(it, L)
    rememberBase(it, snapPrev)
    rememberBase(it, snapNow)
    local base = it._base
    -- a value going back (hidden -> shown, collision off -> on, a bool flipping back) takes back this object's
    -- earlier vote, near or far: it was a passing state (an animation, "busy" flag), not "taken"
    if pcls then
        for _, k in ipairs(backKeys(snapPrev, snapNow)) do unvote(pcls, k, id) end
    end
    local changes = M.diff(snapPrev, snapNow)
    -- a door that vanished is never learned from: "vanish" can not be a door's "unlocked" rule
    if isDoor then
        local keep = {}
        for _, r in ipairs(changes) do if r.type ~= "vanish" then keep[#keep + 1] = r end end
        changes = keep
    end
    if #changes == 0 then return nil end
    -- a bool or number going back to its first-seen value is no vote (near or far), and takes back every vote of
    -- this object for that property. A bool flipping back takes back this object's vote for the other value and
    -- is no vote itself; a number moving on takes back its vote for the earlier value and votes for the new one
    -- (the final state wins)
    local skip = {}
    for i, r in ipairs(changes) do
        if (r.type == "bool" or r.type == "num") and base[r.prop] ~= nil and base[r.prop] == r.is then
            skip[i] = true
            if pcls then
                for k, rec in pairs(pcls) do
                    if (rec.rule.type == "bool" or rec.rule.type == "num") and rec.rule.prop == r.prop then
                        unvote(pcls, k, id)
                    end
                end
            end
        end
    end
    if pcls then
        for i, r in ipairs(changes) do
            if skip[i] then
                -- back to the first-seen value: handled above
            elseif r.type == "bool" then
                if unvote(pcls, ruleKey({ type = "bool", prop = r.prop, is = not r.is }), id) then skip[i] = true end
            elseif r.type == "num" then
                for k, rec in pairs(pcls) do
                    if rec.rule.type == "num" and rec.rule.prop == r.prop and k ~= ruleKey(r) then unvote(pcls, k, id) end
                end
            end
        end
    end
    if next(skip) then
        local keep = {}
        for i, r in ipairs(changes) do if not skip[i] then keep[#keep + 1] = r end end
        changes = keep
        if #changes == 0 then return nil end
    end
    if not near then return nil end                          -- far changes are never learned from
    if it.noLearn == true then return nil end
    if isSeed(it.entry, L, cls) then return nil end           -- profile seed / door: its own rules, nothing to learn
    if not pcls then
        pcls = {}
        pendingOf(L)[cls] = pcls
    end
    local ev = { cls = cls, id = id, t = now, kind = it.kind, rules = changes, rule = changes[1], learned = false,
                 known = false }
    local winner = nil
    for _, r in ipairs(changes) do
        local key = ruleKey(r)
        if knownRule(L, cls, isDoor, key) then
            ev.known = true
            if ev.rule == changes[1] then ev.rule = r end
        else
            local rec = pcls[key]
            if not rec then
                if countKeys(pcls) >= M.MAX_PENDING then break end
                rec = { rule = r, ids = {}, n = 0, events = 0 }
                pcls[key] = rec
            end
            rec.events = rec.events + 1
            if not rec.ids[id] then rec.ids[id] = true; rec.n = rec.n + 1 end
            if not winner and rec.n >= M.LEARN_OBJECTS then winner = rec end
        end
    end
    if winner then
        if writeLearned(L, cls, it.kind, isDoor, winner.rule, winner.events) then
            ev.learned = true
            ev.rule = winner.rule
        end
        pcls[ruleKey(winner.rule)] = nil
    end
    return ev
end

-- learnStep(it, snapPrev, snapNow, playerNear, now, L) -> event | nil
--   it         = {obj=, cls=, id=, kind=, entry=?, noLearn=?}  (kind "door" learns an "unlocked" rule into L.doors)
--   snapPrev/snapNow = snapshot() results (pass {valid=false} as snapNow when the object vanished)
--   playerNear = true/false, or the player's distance in metres (then "near" = within NEAR_M now or in the last
--                NEAR_S seconds, remembered on the item)
--   now        = seconds (same clock every call); L = the learned table (sense_profile shape), changed in place
-- event = {cls, id, t, kind, rules={candidate RULEs}, rule=RULE, learned=true when L just got a new rule (save L),
--          known=true when the change matches an already learned rule}. nil = no change, or far, or not learnable.
-- A rule is learned after agreeing changes on LEARN_OBJECTS different objects of one class, each near the player.
-- A change back on the same object (shown again, bool back) takes its earlier vote away. A bool/number going back
-- to the first value seen on that object (kept per session by L + object id, so a rebuilt item table keeps it;
-- snapshot(it, watch, {L=L}) links it at snapshot time too; resetBase(L) forgets it) never votes and takes back the
-- object's votes for that property. Doors never learn "vanish".
function M.learnStep(it, snapPrev, snapNow, playerNear, now, L)
    local ok, ev = pcall(learnImpl, it, snapPrev, snapNow, playerNear, now, L)
    if not ok then fail("learnStep", ev); return nil end
    return ev
end

-- how many classes L has learned something for (item classes + door classes; same count as
-- sense_profile.learnedCount; for "learned": {"count": n})
function M.learnedCount(L)
    local n = 0
    if type(L) ~= "table" then return 0 end
    if type(L.classes) == "table" then for _ in pairs(L.classes) do n = n + 1 end end
    if type(L.doors) == "table" then for _ in pairs(L.doors) do n = n + 1 end end
    return n
end

-- ---------- doors: "unlocked" flips ----------
-- newDoorTracker() -> tracker (session only). doorStep(tracker, d, now, ctx) -> event | nil
--   d   = {obj=, cls=, id=(stable key, e.g. the full name), entry=profile door entry {unlocked=RULE, pos_prop=,
--          id_prop=, kind=?} or nil (then L.doors[cls].unlocked from ctx.L is used), pos=? (metres, optional)}
--   event = {cls, id, pos={x,y,z} metres, t=now, kind="lock"|"door"}  (sense_target.unlockedTarget input)
-- The first read of a door is only its baseline and never counts. An event fires when a door read locked (false)
-- before and unlocked (true) now. A read that fails (nil) changes nothing. When the position cannot be read the
-- flip is kept pending (baseline stays locked) and tried again next call.
function M.newDoorTracker()
    return { last = {}, n = 0 }
end

local function doorPos(obj, entry, ue)
    local pos = nil
    local pp = type(entry) == "table" and entry.pos_prop or nil
    if type(pp) == "string" and pp ~= "" and obj ~= nil then
        local ok, v = pcall(function() return obj[pp] end)
        if ok and v ~= nil then pos = ue.readVec(v) end
    end
    if not pos then pos = ue.readVec(ue.call(obj, "K2_GetActorLocation")) end
    return pos
end

local function doorImpl(tr, d, now, ctx)
    local ue = ueOf(ctx)
    if type(tr) ~= "table" or type(d) ~= "table" or not ue then return nil end
    local cls = d.cls
    local entry = d.entry
    local rule = type(entry) == "table" and entry.unlocked or nil
    if type(rule) ~= "table" then
        local ld = learnedDoor(type(ctx) == "table" and ctx.L or nil, cls)
        rule = ld and ld.unlocked or nil
    end
    if type(rule) ~= "table" then return nil end
    local key = d.id
    if key == nil then key = ue.fullName(d.obj) end
    if key == nil then return nil end
    key = tostring(key)
    local v = eval(d.obj, rule, ctx, cls)
    if v ~= true and v ~= false then return nil end         -- failed read: no baseline, no change
    if type(tr.last) ~= "table" then tr.last, tr.n = {}, 0 end
    local prev = tr.last[key]
    if prev == nil then
        if (tr.n or 0) >= M.MAX_DOORS then tr.last, tr.n = {}, 0 end
        tr.n = (tr.n or 0) + 1
        tr.last[key] = v
        return nil                                            -- baseline: never an event
    end
    if prev == false and v == true then
        local pos = nil
        if type(d.pos) == "table" and finite(d.pos[1]) and finite(d.pos[2]) and finite(d.pos[3]) then
            pos = { d.pos[1], d.pos[2], d.pos[3] }
        elseif rule.type ~= "vanish" then
            pos = doorPos(d.obj, entry, ue)
        end
        if not pos then return nil end                        -- keep "locked" so the flip is seen again next call
        tr.last[key] = true
        local id = nil
        local ip = type(entry) == "table" and entry.id_prop or nil
        if type(ip) == "string" and ip ~= "" then
            local s = ue.readStr(d.obj, ip)
            if type(s) == "string" and s ~= "" and s ~= "None" then id = s end
        end
        -- "lock" when the profile says so or the class name has "Lock" in it (PBZ MapLockObject...), else "door"
        local kind = "door"
        if (type(entry) == "table" and entry.kind == "lock") or (type(cls) == "string" and cls:find("Lock", 1, true)) then
            kind = "lock"
        end
        return { cls = cls, id = id or key, pos = pos, t = now, kind = kind }
    end
    tr.last[key] = v
    return nil
end

function M.doorStep(tracker, d, now, ctx)
    local ok, ev = pcall(doorImpl, tracker, d, now, ctx)
    if not ok then fail("doorStep", ev); return nil end
    return ev
end

return M
