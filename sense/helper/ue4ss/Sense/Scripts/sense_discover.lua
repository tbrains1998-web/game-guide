-- sense_discover: which actors are items / doors (Unreal, UE4SS). READ-ONLY; every game touch goes through
-- sense_ue (which wraps each one in pcall), and each sense_ue call here is in pcall too.
-- Usage: local discover = require("sense_discover")
--   discover.classify(cls, chain, P, L)  -> kind | "door" | nil, reason[, parent]
--        kind = "pickup" | "collectible". chain = class names from sense_ue.superChain (own class first is fine).
--        reason: "seed", "door_seed", "learned", "learned_door", "seed_parent", "door_parent", "learned_parent",
--        "learned_door_parent", "words", "parent_words", "door_words", "parent_door_words" (found) or
--        "not_items", "exclude", "parent_not_items", "parent_exclude", "default_object", "no_name", "no_words" (nil)
--   discover.entryFor(cls, P, L, session) -> entry | nil   (what tierA gives an actor of exactly this class)
--   discover.tierA(P, L, session)         -> found, secs, counts
--        found = { [id] = {obj=, cls=, kind=, entry=} }   id = GetFullName, cls = the actor's OWN class name.
--        Doors are in it too (kind "door"); the caller keeps them out of the item list. Every 2 s.
--   discover.tierB(P, L, session, now, playerPos, mapName) -> newClasses, secs
--        newClasses = list of {cls=, kind=, reason=, parent=}. One FindAllOf("Actor") + one GetFullName per actor,
--        new classes classified once by words on their name and super chain. Runs only when the profile says
--        discovery mode "auto", there is a player, the map changed or the player moved more than move_m since the
--        last run (the first call counts as a change), and min_interval_s (doubled while a run costs more than
--        budget_s, up to max_interval_s) has passed. Otherwise returns {}, 0 without touching the game.
--   discover.wordHit(name, wordsLower)   -> word | nil    (word at the start of a name part, any case)
--   discover.setLog(fn)
--
-- session (a table the caller keeps for the whole game session; this module owns these keys):
--   session.classes  = { [cls] = {kind=, reason=, parent=} }   classes found by tierB this session (tierA uses them)
--   session.disc     = tierB timing state; session.reqCache/reqCacheN = "require" results per actor id
--   session.ctx      = (optional, caller's) ctx passed to sense_state.evalRule for "require" rules
-- Entries: a profile seed (P.seeds[i], seed=true), a profile door (P.doors[i], seed=true) or a made one:
--   items: {name=, kind="pickup"|"collectible", require={RULE}, gone={RULE}, learned=true|nil, guess=true|nil,
--           learnedRule=the L.classes entry|nil}. Learned classes: gone = learned rules (guess_gone when empty);
--           classes guessed from words: gone = P.guess_gone.
--   doors: {name=, door=true, kind="lock"|"door"|nil (the target kind; nil = sense_state guesses from the name),
--           unlocked=RULE|nil, pos_prop=, id_prop=}. In tierA's result a door's kind is always "door".
-- Profile seeds and doors are used as they are (learned rules are not added to them). An actor of a class that is
-- not known itself (a child class FindAllOf also returned) uses the entry of the name it was found under, like
-- the old main.lua.

local M = {}

M.MAX_ACTORS = 60000          -- tierB looks at no more actors than this in one run
M.MAX_CHAIN_PER_RUN = 400     -- most new classes whose super chain is read in one tierB run (the rest: next run)
M.MAX_SESSION_CLASSES = 64    -- most classes tierB adds for tierA (each costs one FindAllOf every 2 s)
M.REQ_CACHE_MAX = 20000

-- ---------- log ----------
local logFn = function(s) print("[Sense] " .. tostring(s) .. "\n") end
function M.setLog(fn) if type(fn) == "function" then logFn = fn end end
local function log(s) pcall(logFn, s) end
local logged = {}
local function logOnce(key, s) if not logged[key] then logged[key] = true; log(s) end end

-- ---------- other modules ----------
local deps = {}
local function dep(name)
    if deps[name] then return deps[name] end
    local ok, m = pcall(require, name)
    if ok and type(m) == "table" then deps[name] = m; return m end
    return nil
end
function M._setDep(name, mod) deps[name] = mod end   -- tests

local function notItem(P, cls)
    local prof = dep("sense_profile")
    if prof and prof.notItem then
        local ok, v = pcall(prof.notItem, P, cls)
        return ok and v == true
    end
    if type(P) == "table" and type(P.notItemSet) == "table" and P.notItemSet[cls] then return true end
    return false
end

-- ---------- sense_ue calls (each in pcall) ----------
local function ueCall(fname, ...)
    local ue = dep("sense_ue")
    if not ue or type(ue[fname]) ~= "function" then return nil end
    local ok, a, b = pcall(ue[fname], ...)
    if ok then return a, b end
    return nil
end

local function isTemplate(id)
    return id:find("Default__", 1, true) ~= nil or id:find("TrashedPackage", 1, true) ~= nil
end

local function classOfId(id)
    local c = ueCall("classOf", id)
    if type(c) == "string" and c ~= "" then return c end
    return id:match("^(%S+)")
end

local function fullNameOf(obj)
    local s = ueCall("fullName", obj)
    if type(s) == "string" and s ~= "" and s ~= "?" then return s end
    return nil
end

-- ---------- words ----------
-- positions where a name part starts: after _ or another non-letter/digit, lower->Upper, letter<->digit, and the
-- last capital of a run of capitals that is followed by a small letter ("UIChest" -> UI|Chest)
local function partStarts(s)
    local starts = {}
    local n = #s
    for i = 1, n do
        local c = s:sub(i, i)
        if c:match("%w") then
            local p = i > 1 and s:sub(i - 1, i - 1) or ""
            local nx = s:sub(i + 1, i + 1)
            local st = false
            if p == "" or not p:match("%w") then st = true
            elseif p:match("%l") and c:match("%u") then st = true
            elseif (p:match("%d") and c:match("%a")) or (p:match("%a") and c:match("%d")) then st = true
            elseif p:match("%u") and c:match("%u") and nx:match("^%l") then st = true end
            if st then starts[#starts + 1] = i end
        end
    end
    return starts
end

-- the first word of wordsLower that starts at the start of a name part of `name` (any case), or nil
function M.wordHit(name, wordsLower)
    if type(name) ~= "string" or type(wordsLower) ~= "table" or #wordsLower == 0 then return nil end
    local low = name:lower()
    local starts = partStarts(name)
    for _, w in ipairs(wordsLower) do
        local lw = #w
        for _, i in ipairs(starts) do
            if low:sub(i, i + lw - 1) == w then return w end
        end
    end
    return nil
end
local wordHit = M.wordHit

local function chainNames(cls, chain)
    local out = {}
    if type(chain) ~= "table" then return out end
    for _, c in ipairs(chain) do
        local n = c
        if type(c) == "table" then n = c.name end
        if type(n) == "string" and n ~= "" and n ~= cls then out[#out + 1] = n end
    end
    return out
end

local function lcls(L) return type(L) == "table" and type(L.classes) == "table" and L.classes or {} end
local function ldoors(L) return type(L) == "table" and type(L.doors) == "table" and L.doors or {} end
local KINDS = { pickup = true, collectible = true }

-- a name known by profile/learned file: kind, reason
local function knownName(n, P, L)
    if P.seedByName and P.seedByName[n] then return P.seedByName[n].kind, "seed" end
    if P.doorByName and P.doorByName[n] then return "door", "door_seed" end
    local lc = lcls(L)[n]
    if type(lc) == "table" and KINDS[lc.kind] then return lc.kind, "learned" end
    local ld = ldoors(L)[n]
    if type(ld) == "table" and ld.unlocked then return "door", "learned_door" end
    return nil
end

local function wordsKind(n, P)
    if wordHit(n, P.collectibleWordsL) then return "collectible" end
    if wordHit(n, P.itemWordsL) then return "pickup" end
    if wordHit(n, P.doorWordsL) then return "door" end
    return nil
end

function M.classify(cls, chain, P, L)
    if type(cls) ~= "string" or cls == "" then return nil, "no_name" end
    if type(P) ~= "table" then return nil, "no_profile" end
    if cls:find("Default__", 1, true) then return nil, "default_object" end
    if notItem(P, cls) then return nil, "not_items" end
    local k, why = knownName(cls, P, L)
    if k then return k, why end
    if wordHit(cls, P.excludeWordsL) then return nil, "exclude" end
    local parents = chainNames(cls, chain)
    for _, p in ipairs(parents) do
        if notItem(P, p) then return nil, "parent_not_items", p end
        local pk, pwhy = knownName(p, P, L)
        if pk then return pk, pwhy .. "_parent", p end
        if wordHit(p, P.excludeWordsL) then return nil, "parent_exclude", p end
    end
    local wk = wordsKind(cls, P)
    if wk then return wk, wk == "door" and "door_words" or "words" end
    for _, p in ipairs(parents) do
        local pk = wordsKind(p, P)
        if pk then return pk, pk == "door" and "parent_door_words" or "parent_words", p end
    end
    return nil, "no_words"
end

-- ---------- entries ----------
local function emptyList(t) return type(t) ~= "table" or #t == 0 end

local function entryForDepth(cls, P, L, session, depth)
    if type(cls) ~= "string" or depth > 4 then return nil end
    -- profile seeds and doors are used as they are (the profile is the boss; sense_state does not add learned
    -- rules to seeds either)
    local seed = P.seedByName and P.seedByName[cls]
    if seed then return seed end
    local door = P.doorByName and P.doorByName[cls]
    if door then return door end
    local lc = lcls(L)[cls]
    local ld = ldoors(L)[cls]
    if type(lc) == "table" and KINDS[lc.kind] then
        local gone = lc.gone
        if emptyList(gone) then gone = P.guess_gone or {} end
        return { name = cls, kind = lc.kind, require = {}, gone = gone, learned = true, learnedRule = lc }
    end
    if type(ld) == "table" and ld.unlocked then
        return { name = cls, door = true, unlocked = ld.unlocked, learned = true, learnedRule = ld }
    end
    local sc = type(session) == "table" and type(session.classes) == "table" and session.classes[cls] or nil
    if type(sc) == "table" then
        if sc.parent then
            local pe = entryForDepth(sc.parent, P, L, session, depth + 1)
            if pe then return pe end
        end
        if sc.kind == "door" then return { name = cls, door = true, guess = true } end
        if KINDS[sc.kind] then return { name = cls, kind = sc.kind, require = {}, gone = P.guess_gone or {}, guess = true } end
    end
    return nil
end

function M.entryFor(cls, P, L, session)
    if type(P) ~= "table" then return nil end
    return entryForDepth(cls, P, L, session, 0)
end

-- ---------- "require" rules (e.g. LootItemID set), read once per actor like the old main.lua ----------
-- true = counts. A definite answer (true/false) is remembered per actor id; an unreadable value (nil) is not
-- remembered and the actor is not counted this time (tried again on the next tierA).
local function requireOk(id, obj, entry, session)
    local req = entry.require
    if type(req) ~= "table" or #req == 0 then return true end
    local cache = session.reqCache
    if type(cache) ~= "table" then cache = {}; session.reqCache = cache; session.reqCacheN = 0 end
    local known = cache[id]
    if known ~= nil then return known end
    local st = dep("sense_state")
    local result
    if st and type(st.evalRule) == "function" then
        result = true
        for _, r in ipairs(req) do
            local ok, v = pcall(st.evalRule, obj, r, session.ctx or {})
            if not ok or v == nil then return false end     -- unknown: not counted, not remembered
            if v ~= true then result = false; break end
        end
    elseif st and type(st.requireOk) == "function" then
        local ok, v = pcall(st.requireOk, obj, entry)
        if not ok or v ~= true then return false end        -- only a yes is remembered here
        result = true
    else
        logOnce("nostate", "discover: sense_state.lua is missing, so classes with 'require' rules are not counted")
        return false
    end
    if (session.reqCacheN or 0) >= M.REQ_CACHE_MAX then cache = {}; session.reqCache = cache; session.reqCacheN = 0 end
    cache[id] = result
    session.reqCacheN = (session.reqCacheN or 0) + 1
    return result
end

local function sortedKeys(t)
    local out = {}
    for k in pairs(t) do if type(k) == "string" then out[#out + 1] = k end end
    table.sort(out)
    return out
end

-- ---------- tier A: FindAllOf per known class name ----------
function M.tierA(P, L, session)
    session = type(session) == "table" and session or {}
    local found, counts, secsAll = {}, {}, 0
    if type(P) ~= "table" then return found, 0, counts end
    if not dep("sense_ue") then
        logOnce("noue", "discover: sense_ue.lua is missing, nothing can be found")
        return found, 0, counts
    end
    local names, nameEntry = {}, {}
    local function addName(n)
        if type(n) == "string" and n ~= "" and not nameEntry[n] then
            local e = M.entryFor(n, P, L, session)
            if e then names[#names + 1] = n; nameEntry[n] = e end
        end
    end
    for _, s in ipairs(P.seeds or {}) do addName(s.name) end
    for _, d in ipairs(P.doors or {}) do addName(d.name) end
    for _, n in ipairs(sortedKeys(lcls(L))) do addName(n) end
    for _, n in ipairs(sortedKeys(ldoors(L))) do addName(n) end
    if type(session.classes) == "table" then
        for _, n in ipairs(sortedKeys(session.classes)) do addName(n) end
    end
    for _, n in ipairs(names) do
        local list, secs = ueCall("findAll", n)
        if type(secs) == "number" and secs > 0 then secsAll = secsAll + secs end
        if type(list) == "table" then
            counts[n] = 0
            for _, obj in ipairs(list) do
                if ueCall("valid", obj) == true then
                    local id = fullNameOf(obj)
                    if id and not isTemplate(id) and not found[id] then
                        local cls = classOfId(id)
                        if cls and not notItem(P, cls) then
                            -- the actor's OWN class wins; a child class without its own entry uses the found-under one
                            local entry = M.entryFor(cls, P, L, session) or nameEntry[n]
                            if entry and requireOk(id, obj, entry, session) then
                                local kind = entry.door and "door" or entry.kind
                                found[id] = { obj = obj, cls = cls, kind = kind, entry = entry }
                                counts[n] = counts[n] + 1
                            end
                        end
                    end
                end
            end
        end
    end
    return found, secsAll, counts
end

-- ---------- tier B: discovery over all actors (rare, budgeted) ----------
local function dist3(a, b)
    if type(a) ~= "table" or type(b) ~= "table" then return 0 end
    local dx, dy, dz = (a[1] or 0) - (b[1] or 0), (a[2] or 0) - (b[2] or 0), (a[3] or 0) - (b[3] or 0)
    return math.sqrt(dx * dx + dy * dy + dz * dz)
end

local function countKeys(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

local function isPos(p)
    return type(p) == "table" and type(p[1]) == "number" and type(p[2]) == "number" and type(p[3]) == "number"
end

function M.tierB(P, L, session, now, playerPos, mapName)
    local new = {}
    if type(P) ~= "table" or type(P.discovery) ~= "table" or P.discovery.mode ~= "auto" then return new, 0 end
    if type(session) ~= "table" or type(now) ~= "number" then return new, 0 end
    local d = P.discovery
    session.classes = type(session.classes) == "table" and session.classes or {}
    local st = session.disc
    if type(st) ~= "table" then
        st = { lastRun = nil, lastPos = nil, lastMap = nil, interval = d.min_interval_s, known = {}, pending = true,
               runs = 0, lastSecs = 0 }
        session.disc = st
    end
    -- triggers (remembered until a run is allowed)
    if mapName ~= nil and mapName ~= st.lastMap and st.runs > 0 then st.pending = true end
    if isPos(playerPos) and isPos(st.lastPos) and dist3(playerPos, st.lastPos) > d.move_m then st.pending = true end
    if not st.pending or not isPos(playerPos) then return new, 0 end
    local interval = math.max(d.min_interval_s, st.interval or d.min_interval_s)
    if st.lastRun and now - st.lastRun < interval then return new, 0 end
    if not dep("sense_ue") then
        logOnce("noue", "discover: sense_ue.lua is missing, nothing can be found")
        return new, 0
    end
    -- one FindAllOf("Actor") + one GetFullName per actor; remember one sample actor per class not seen before
    local t0 = os.clock()
    local actors = ueCall("findAll", "Actor")
    local nActors, samples, order = 0, {}, {}
    if type(actors) == "table" then
        for i, obj in ipairs(actors) do
            if i > M.MAX_ACTORS then break end
            nActors = i
            local id = fullNameOf(obj)
            if id and not isTemplate(id) then
                local cls = classOfId(id)
                if cls and st.known[cls] == nil and samples[cls] == nil then
                    samples[cls] = obj
                    order[#order + 1] = cls
                end
            end
        end
    end
    -- classify each new class once (one super chain walk per class, cached by sense_ue)
    local nSession = countKeys(session.classes)
    local items, doors = 0, 0
    for k, cls in ipairs(order) do
        if k > M.MAX_CHAIN_PER_RUN then break end          -- the rest is looked at in a later run
        local chain = ueCall("superChain", samples[cls], cls)
        local kind, reason, parent = M.classify(cls, chain, P, L)
        st.known[cls] = kind or false
        -- seeds, profile doors and learned classes are in tierA already; only new finds are added
        if kind and reason ~= "seed" and reason ~= "door_seed" and reason ~= "learned" and reason ~= "learned_door" then
            if nSession < M.MAX_SESSION_CLASSES then
                session.classes[cls] = { kind = kind, reason = reason, parent = parent }
                nSession = nSession + 1
                new[#new + 1] = { cls = cls, kind = kind, reason = reason, parent = parent }
                if kind == "door" then doors = doors + 1 else items = items + 1 end
            else
                logOnce("cap", "discover: more than " .. M.MAX_SESSION_CLASSES .. " guessed classes this session; " ..
                    "the rest are ignored (add exclude_words or not_items to the profile)")
            end
        end
    end
    local secs = os.clock() - t0
    if secs < 0 then secs = 0 end
    st.lastRun, st.lastMap, st.pending, st.runs, st.lastSecs = now, mapName, false, st.runs + 1, secs
    st.lastPos = { playerPos[1], playerPos[2], playerPos[3] }
    if secs > d.budget_s then
        st.interval = math.min(math.max(interval * 2, d.min_interval_s), d.max_interval_s)
    else
        st.interval = math.max(d.min_interval_s, interval / 2)
    end
    if #order > M.MAX_CHAIN_PER_RUN then st.pending = true end   -- more classes left: look again after the interval
    for _, e in ipairs(new) do
        log(string.format("discover: new %s class %s (%s%s)", e.kind, e.cls, e.reason, e.parent and (" " .. e.parent) or ""))
    end
    log(string.format("discover: %d actors, %d new classes looked at, %d new item and %d new door classes in %.3f s; " ..
        "next run earliest in %d s", nActors, math.min(#order, M.MAX_CHAIN_PER_RUN), items, doors, secs,
        math.floor(st.interval)))
    return new, secs
end

return M
