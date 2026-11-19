-- sense_ue.lua: every Unreal (UE4SS) game touch of the Sense helper, in one place. READ-ONLY.
-- main.lua loads it as the module "sense_ue"; it returns a table of functions. Nothing here is global.
--
-- Rules this file keeps (game crash history, see the main.lua comments):
--   * every touch of a game object is inside pcall; a failed read gives nil, never an error
--   * only calls that are proven safe on PBZ's UE4SS build: FindAllOf(name), obj:IsValid(), obj:GetFullName(),
--     obj:GetClass(), GetFName():ToString(), obj[prop] for Bool/Name/number/Object properties, no-argument
--     UFunctions, UStruct ForEachProperty and GetSuperStruct
--   * never the walk over every object, never the parameter-wrapper value read, never the array element
--     walk, never a hook of any kind (those crashed PBZ or are unverified)
--   * array properties: only their LENGTH is read (arrayAnyTrue), entries are never read
--   * only a real Lua boolean counts as a bool (a number 1 is not "true")
--   * this file never writes anything (no files, no game values)
-- The caller (main.lua) runs all of these on the game thread, with its in-flight guard.
-- Class facts (super chain, property lists, missing functions) are cached per class name; classes do not change
-- while the game runs. Caches are capped so they never grow without end.

local M = {}

M.VERSION   = "sense_ue/1"
M.MAX_SUPER = 8      -- most class levels walked (own class counts as level 1)
M.MAX_PROPS = 512    -- most properties kept per class (all levels together)
M.MAX_WATCH = 16     -- most watched properties per class
M.MAX_CACHE = 4096   -- most classes kept in one cache; when full the cache starts over
M.FAIL_LIMIT = 3     -- a class walk that breaks half way this often is cached as far as it got
-- engine classes the property walk stops at (their properties are known and not interesting)
M.STOP_CLASSES = { Object = true, Actor = true, ActorComponent = true, SceneComponent = true, PrimitiveComponent = true }
-- default words for watchProps (lower case; a BoolProperty whose name contains one of them is watched)
M.WATCH_WORDS = { "open", "loot", "collect", "taken", "pick", "unlock", "lock", "destroy", "destory", "interact",
                  "used", "activ", "complete", "finish", "gather", "empty", "state" }

-- ---------- small pure helpers ----------
local function finite(n)
    return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

-- ---------- basic safe access ----------

-- true only when obj answers IsValid() with a real true
function M.valid(obj)
    if obj == nil then return false end
    local ok, v = pcall(function() return obj:IsValid() end)
    return ok and v == true
end
local valid = M.valid

-- call a no-argument UFunction; nil when it is missing or fails
function M.call(obj, fname)
    if obj == nil or type(fname) ~= "string" then return nil end
    local ok, r = pcall(function() return obj[fname](obj) end)
    if ok then return r end
    return nil
end

-- UE FVector (cm) -> {x, y, z} metres, or nil (missing, not numbers, NaN or infinite)
function M.readVec(v)
    if v == nil then return nil end
    local ok, x, y, z = pcall(function() return v.X, v.Y, v.Z end)
    if ok and finite(x) and finite(y) and finite(z) then return { x / 100, y / 100, z / 100 } end
    return nil
end

-- UE FRotator -> {pitch, yaw, roll} degrees, or nil
function M.readRot(r)
    if r == nil then return nil end
    local ok, p, y, ro = pcall(function() return r.Pitch, r.Yaw, r.Roll end)
    if ok and finite(p) and finite(y) and finite(ro) then return { p, y, ro } end
    return nil
end

-- the actor's location in metres {x, y, z} (K2_GetActorLocation), or nil
function M.actorPos(obj)
    return M.readVec(M.call(obj, "K2_GetActorLocation"))
end

-- GetFullName() ("BP_Chest_C /Game/Map.Map:PersistentLevel.BP_Chest_C_12"), or nil
function M.fullName(obj)
    if obj == nil then return nil end
    local ok, s = pcall(function() return obj:GetFullName() end)
    if ok and type(s) == "string" and s ~= "" then return s end
    return nil
end

-- class name = the first word of a full name. Takes the full name (string) or an object (then one GetFullName).
-- nil when unknown.
function M.classOf(x)
    local s = x
    if type(s) ~= "string" then s = M.fullName(x) end
    if type(s) ~= "string" then return nil end
    return s:match("^(%S+)")
end

-- short class name through GetClass():GetFName():ToString() (3 game calls; classOf(fullName) is 1), or nil
function M.className(obj)
    if obj == nil then return nil end
    local ok, s = pcall(function() return obj:GetClass():GetFName():ToString() end)
    if ok and type(s) == "string" and s ~= "" then return s end
    return nil
end

-- class default objects and unloaded level parts are never real world actors
function M.isTemplateName(full)
    if type(full) ~= "string" then return true end
    return full:find("Default__", 1, true) ~= nil or full:find("TrashedPackage", 1, true) ~= nil
end

-- FindAllOf(name) in pcall, timed. Returns the list (a table; may also hold child classes) or nil, and the seconds
-- it took (os.clock). A class the game does not have gives nil.
function M.findAll(name)
    if type(name) ~= "string" or name == "" then return nil, 0 end
    local t0 = os.clock()
    local ok, list = pcall(FindAllOf, name)
    local secs = os.clock() - t0
    if secs < 0 then secs = 0 end
    if ok and type(list) == "table" then return list, secs end
    return nil, secs
end

-- ---------- property reads (obj[prop] in pcall, type checked) ----------

local function rawProp(obj, prop)
    if obj == nil or type(prop) ~= "string" then return false, nil end
    return pcall(function() return obj[prop] end)
end

-- a BoolProperty: true/false, or nil when missing, unreadable or not a real boolean
function M.readBool(obj, prop)
    local ok, v = rawProp(obj, prop)
    if ok and type(v) == "boolean" then return v end
    return nil
end

-- a number property (Int/Float/Byte/Enum...): the number, or nil (also nil for NaN / infinite)
function M.readNum(obj, prop)
    local ok, v = rawProp(obj, prop)
    if ok and finite(v) then return v end
    return nil
end

-- text of a Name/String/Text value: a Lua string as is, otherwise its ToString(); nil when neither works
local function textOf(v)
    if type(v) == "string" then return v end
    if v == nil or type(v) == "boolean" or type(v) == "number" then return nil end
    local ok, s = pcall(function() return v:ToString() end)
    if ok and type(s) == "string" then return s end
    return nil
end

-- a NameProperty as text ("None" stays "None"; the caller decides what counts as set), or nil
function M.readName(obj, prop)
    local ok, v = rawProp(obj, prop)
    if not ok then return nil end
    return textOf(v)
end

-- a StrProperty (or TextProperty) as text, or nil
function M.readStr(obj, prop)
    local ok, v = rawProp(obj, prop)
    if not ok then return nil end
    return textOf(v)
end

-- the actor's hidden flag: bHidden, else IsHidden(); nil when neither answers with a boolean
function M.readHidden(obj)
    local h = M.readBool(obj, "bHidden")
    if h ~= nil then return h end
    if obj == nil then return nil end
    local ok, v = pcall(function() return obj:IsHidden() end)
    if ok and type(v) == "boolean" then return v end
    return nil
end

-- An ArrayProperty "are any entries true?" check, with main.lua's old readArray rules:
-- only the array's LENGTH is read. The entries are never read (that path crashed PBZ twice and pcall cannot catch
-- it), so a non-empty array is "unknown". Returns result, length:
--   false, 0      the array is empty (nothing can be true)
--   nil,   n      n > 0 entries (unknown), or more than max entries
--   nil,   nil    the property is missing or its length cannot be read
-- A failed or unknown read never counts as true, so it can never hide an item on its own.
function M.arrayAnyTrue(obj, prop, max)
    max = finite(max) and max or 64
    local ok, arr = rawProp(obj, prop)
    if not ok or arr == nil or type(arr) == "boolean" or type(arr) == "number" or type(arr) == "string" then
        return nil, nil
    end
    local okn, n = pcall(function() return arr:GetArrayNum() end)
    if not (okn and finite(n)) then okn, n = pcall(function() return #arr end) end
    if not (okn and finite(n)) then return nil, nil end
    if n <= 0 then return false, n end
    if n > max then return nil, n end
    return nil, n
end

-- ---------- per-class "this function is missing" memory ----------
local fnState = {}        -- "cls|fname" -> "ok" | "missing" | number of failures so far
local fnStateN = 0

-- call a no-argument UFunction with a per-class memory: once a class has failed FAIL_LIMIT times on fname
-- (and never answered), it is not tried again for that class. Returns value|nil, missing (true = skipped because
-- the class does not have it). Without cls it is a plain call().
function M.callCls(obj, cls, fname)
    if type(cls) ~= "string" or type(fname) ~= "string" then return M.call(obj, fname), false end
    local key = cls .. "|" .. fname
    local st = fnState[key]
    if st == "missing" then return nil, true end
    if obj == nil then return nil, false end
    local ok, r = pcall(function() return obj[fname](obj) end)
    if ok then
        if st ~= "ok" then
            if st == nil then fnStateN = fnStateN + 1 end
            fnState[key] = "ok"
        end
        return r, false
    end
    if st ~= "ok" then
        if fnStateN > M.MAX_CACHE then fnState, fnStateN, st = {}, 0, nil end
        if st == nil then fnStateN = fnStateN + 1 end
        local n = (type(st) == "number" and st or 0) + 1
        if n >= M.FAIL_LIMIT then fnState[key] = "missing" else fnState[key] = n end
    end
    return nil, false
end

-- ---------- class walks (cached per class name) ----------
local chainCache, chainN = {}, 0      -- cls -> {names}
local propsCache, propsN = {}, 0      -- cls -> {{name=, type=, cls=}}
-- props list (the cached table itself, weak key) -> {words key -> {names, types}}; goes away with its props list
local watchCache = setmetatable({}, { __mode = "k" })
local walkFails = {}                  -- "chain|cls" / "props|cls" -> failures so far
M.stats = { chainWalks = 0, propWalks = 0 }

local function structName(struct)
    local ok, s = pcall(function() return struct:GetFName():ToString() end)
    if ok and type(s) == "string" and s ~= "" then return s end
    return nil
end

local function superOf(struct)
    local ok, s = pcall(function() return struct:GetSuperStruct() end)
    if not ok then return nil, true end           -- error: the walk broke
    if valid(s) then return s, false end
    return nil, false                             -- no parent (top reached)
end

local function classObj(obj)
    if obj == nil then return nil end
    local ok, c = pcall(function() return obj:GetClass() end)
    if ok and valid(c) then return c end
    return nil
end

local function putCache(cache, n, key, val)
    if n >= M.MAX_CACHE then cache, n = {}, 0 end
    if cache[key] == nil then n = n + 1 end
    cache[key] = val
    return cache, n
end

-- a broken walk is cached only after FAIL_LIMIT breaks of the same class (it is a class problem, not a one-off)
local function failedOften(kind, cls)
    local k = kind .. "|" .. cls
    local n = (walkFails[k] or 0) + 1
    walkFails[k] = n
    if n >= M.FAIL_LIMIT then walkFails[k] = nil; return true end
    return false
end

-- cls given = the caller already knows the class name (first word of the full name); then a cached class needs
-- no game call at all. Without cls it costs one GetFullName.
local function keyOf(obj, cls)
    if type(cls) == "string" and cls ~= "" then return cls end
    return M.classOf(M.fullName(obj))       -- never a plain string taken as a full name here
end

-- the class names from obj's own class up through its parents (GetSuperStruct), at most MAX_SUPER, e.g.
-- {"BP_Chest_C", "BP_InteractBase_C", "Actor", "Object"}. Cached per class name. nil when the class cannot be read.
-- The returned table is shared (cached): do not change it.
function M.superChain(obj, cls)
    local key = keyOf(obj, cls)
    if key and chainCache[key] then return chainCache[key] end
    local struct = classObj(obj)
    if not struct then return nil end
    M.stats.chainWalks = M.stats.chainWalks + 1
    local names, broken = {}, false
    while struct and #names < M.MAX_SUPER do
        local n = structName(struct)
        if not n then broken = true; break end
        names[#names + 1] = n
        local s, err = superOf(struct)
        if err then broken = true; break end
        struct = s
    end
    if #names == 0 then return nil end
    -- the object's own class name wins as the cache key (a caller's cls that does not match is not trusted)
    local own = names[1]
    if not broken or failedOften("chain", own) then
        chainCache, chainN = putCache(chainCache, chainN, own, names)
    end
    return names
end

-- every property of obj's class and its parents, stopping before the engine classes (Actor, Object, components),
-- at most MAX_SUPER levels and MAX_PROPS properties: list of {name=, type=, cls=} ("CurSwitchState",
-- "BoolProperty", "BP_Chest_C"). Cached per class name. nil when the class cannot be read.
-- The returned table is shared (cached): do not change it.
function M.propsOf(obj, cls)
    local key = keyOf(obj, cls)
    if key and propsCache[key] then return propsCache[key] end
    local struct = classObj(obj)
    if not struct then return nil end
    M.stats.propWalks = M.stats.propWalks + 1
    local list, seen, own, broken, level = {}, {}, nil, false, 0
    while struct and level < M.MAX_SUPER and #list < M.MAX_PROPS do
        local sname = structName(struct)
        if not sname then broken = true; break end
        own = own or sname
        if M.STOP_CLASSES[sname] then break end
        local okf = pcall(function()
            struct:ForEachProperty(function(prop)
                local okp, pn = pcall(function() return prop:GetFName():ToString() end)
                local okt, pt = pcall(function() return prop:GetClass():GetFName():ToString() end)
                if okp and type(pn) == "string" and pn ~= "" and not seen[pn] then
                    seen[pn] = true
                    list[#list + 1] = { name = pn, type = (okt and type(pt) == "string") and pt or "?", cls = sname }
                end
                if #list >= M.MAX_PROPS then return true end   -- stops the walk
                return nil
            end)
        end)
        if not okf then broken = true end                       -- keep going with the parents
        local s, err = superOf(struct)
        if err then broken = true; break end
        struct = s
        level = level + 1
    end
    if not own then return nil end
    if not broken or failedOften("props", own) then
        propsCache, propsN = putCache(propsCache, propsN, own, list)
    end
    return list
end

local BOOL_TYPES  = { BoolProperty = true }
local STATE_TYPES = { ByteProperty = true, EnumProperty = true }

-- the properties worth watching for "taken/opened": BoolProperty whose name contains one of words (any case;
-- default WATCH_WORDS), plus Byte/Enum properties whose name contains "state". At most MAX_WATCH, in class order.
-- Never Array/Map/Set/Struct/Object properties. Returns names (list of strings, maybe empty) and
-- types ({name -> "bool" | "num"}): read "bool" with readBool and "num" with readNum.
function M.watchProps(obj, words, cls)
    words = type(words) == "table" and words or M.WATCH_WORDS
    local props = M.propsOf(obj, cls)
    if not props then return {}, {} end
    local low = {}
    for _, w in ipairs(words) do if type(w) == "string" and w ~= "" then low[#low + 1] = w:lower() end end
    local wkey = table.concat(low, "|")
    local byWords = watchCache[props]
    local hit = byWords and byWords[wkey]
    if hit then return hit.names, hit.types end
    local names, types = {}, {}
    for _, p in ipairs(props) do
        if #names >= M.MAX_WATCH then break end
        local pl = p.name:lower()
        if BOOL_TYPES[p.type] then
            for _, w in ipairs(low) do
                if pl:find(w, 1, true) then names[#names + 1] = p.name; types[p.name] = "bool"; break end
            end
        elseif STATE_TYPES[p.type] and pl:find("state", 1, true) then
            names[#names + 1] = p.name; types[p.name] = "num"
        end
    end
    if not byWords then byWords = {}; watchCache[props] = byWords end
    byWords[wkey] = { names = names, types = types }
    return names, types
end

-- the general "is it still there?" signals of an actor:
--   valid     = IsValid() answered true
--   hidden    = bHidden / IsHidden() (nil = unknown)
--   collision = GetActorEnableCollision() (nil = unknown or the class does not have it)
-- An invalid actor gives {valid=false} only (nothing else is read).
function M.actorSignals(obj, cls)
    if not valid(obj) then return { valid = false } end
    local col = M.callCls(obj, cls or M.classOf(obj), "GetActorEnableCollision")
    if type(col) ~= "boolean" then col = nil end
    return { valid = true, hidden = M.readHidden(obj), collision = col }
end

-- forget every class fact (tests; never needed in game)
function M.resetCaches()
    chainCache, chainN, propsCache, propsN = {}, 0, {}, 0
    watchCache = setmetatable({}, { __mode = "k" })
    walkFails, fnState, fnStateN = {}, {}, 0
    M.stats.chainWalks, M.stats.propWalks = 0, 0
end

return M
