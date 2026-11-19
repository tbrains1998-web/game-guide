-- sense_json: tiny JSON writer + reader for the Sense helper (pure Lua 5.4, no load()).
-- Usage: local json = require("sense_json")
--   json.encode(v)       -> text. Tables are objects unless marked with json.array(); json.NULL -> null.
--                           Floats are written with 3 decimals, NaN/inf -> null (same output as the old main.lua).
--   json.array(t)        -> t marked as a JSON array (so holes or empty tables can never turn into objects).
--   json.NULL            -> explicit null value.
--   json.isArray(t)      -> true when t is marked as an array.
--   json.decode(text)    -> value | nil, err. Never raises, whatever the input.
--                           Arrays come back marked with json.array (so encode(decode(x)) keeps them arrays).
--                           null inside an array -> json.NULL (keeps the positions); null as an object value ->
--                           the key is left out (so "if P.target then" works); a top-level null -> json.NULL.
--                           Integers without a fraction/exponent come back as Lua integers.

local json = {}

local ARRAY = {}                                    -- metatable that marks a table as a JSON array
local NULL = setmetatable({}, { __name = "null", __tostring = function() return "null" end })
json.ARRAY = ARRAY
json.NULL = NULL

function json.array(t) return setmetatable(t or {}, ARRAY) end
function json.isArray(t) return type(t) == "table" and getmetatable(t) == ARRAY end

-- ---------- writer ----------
local MAX_ENCODE_DEPTH = 64   -- deeper (or a loop of tables) -> written as null instead of overflowing

local function jsonString(s)
    s = s:gsub('[%c"\\]', function(c)
        if c == '"' then return '\\"' elseif c == "\\" then return "\\\\"
        elseif c == "\n" then return "\\n" elseif c == "\r" then return "\\r" elseif c == "\t" then return "\\t"
        else return string.format("\\u%04x", c:byte()) end
    end)
    return '"' .. s .. '"'
end

local function isInteger(n)
    if math.type then return math.type(n) == "integer" end
    return false
end

local function jsonNumber(n)
    if n ~= n or n == math.huge or n == -math.huge then return "null" end
    if isInteger(n) then return string.format("%d", n) end
    return string.format("%.3f", n)
end

local function toJson(v, depth)
    local t = type(v)
    if v == nil or v == NULL then return "null"
    elseif t == "boolean" then return v and "true" or "false"
    elseif t == "number" then return jsonNumber(v)
    elseif t == "string" then return jsonString(v)
    elseif t == "table" then
        if depth > MAX_ENCODE_DEPTH then return "null" end
        local parts = {}
        if getmetatable(v) == ARRAY then
            for i = 1, #v do parts[i] = toJson(v[i], depth + 1) end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = k end
        table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
        for i, k in ipairs(keys) do parts[i] = jsonString(tostring(k)) .. ":" .. toJson(v[k], depth + 1) end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return "null"
end

function json.encode(v)
    local ok, s = pcall(toJson, v, 0)
    if ok then return s end
    return "null"
end

-- ---------- reader ----------
local MAX_DECODE_DEPTH = 200

local FAIL = {}   -- marker for our own errors (anything else caught is reported as "internal")

local function fail(pos, msg)
    error({ [FAIL] = true, msg = msg .. " at char " .. tostring(pos) }, 0)
end

local function utf8char(cp)
    if cp < 0x80 then return string.char(cp)
    elseif cp < 0x800 then
        return string.char(0xC0 | (cp >> 6), 0x80 | (cp & 0x3F))
    elseif cp < 0x10000 then
        return string.char(0xE0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3F), 0x80 | (cp & 0x3F))
    else
        return string.char(0xF0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3F), 0x80 | ((cp >> 6) & 0x3F), 0x80 | (cp & 0x3F))
    end
end

local ESC = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }

local function skipWs(s, i)
    local _, e = s:find("^[ \t\r\n]*", i)
    return e + 1
end

local parseValue

local function parseString(s, i)   -- s:sub(i,i) == '"'
    local buf, n = {}, 0
    local j = i + 1
    while true do
        local a, b = s:find('^[^"\\%c]+', j)
        if a then n = n + 1; buf[n] = s:sub(a, b); j = b + 1 end
        local c = s:sub(j, j)
        if c == '"' then
            return table.concat(buf), j + 1
        elseif c == "\\" then
            local e = s:sub(j + 1, j + 1)
            if ESC[e] then
                n = n + 1; buf[n] = ESC[e]; j = j + 2
            elseif e == "u" then
                local hex = s:match("^%x%x%x%x", j + 2)
                if not hex then fail(j, "bad \\u escape") end
                local cp = tonumber(hex, 16)
                j = j + 6
                if cp >= 0xD800 and cp <= 0xDBFF then
                    local lo = s:match("^\\u(%x%x%x%x)", j)
                    local lv = lo and tonumber(lo, 16)
                    if lv and lv >= 0xDC00 and lv <= 0xDFFF then
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lv - 0xDC00)
                        j = j + 6
                    else
                        cp = 0xFFFD   -- lone high surrogate
                    end
                elseif cp >= 0xDC00 and cp <= 0xDFFF then
                    cp = 0xFFFD       -- lone low surrogate
                end
                n = n + 1; buf[n] = utf8char(cp)
            else
                fail(j, "bad escape")
            end
        elseif c == "" then
            fail(j, "unfinished string")
        else
            fail(j, "control character in string")
        end
    end
end

local function parseNumber(s, i)
    local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", i)
    if not num then fail(i, "bad number") end
    -- strict JSON shape check
    local int, frac, exp = num:match("^(-?%d+)(%.?%d*)([eE]?[-+]?%d*)$")
    if not int or int:match("^-?0%d") or frac == "." or exp:match("^[eE][-+]?$") or (exp ~= "" and not exp:match("^[eE][-+]?%d+$")) then
        fail(i, "bad number")
    end
    local v
    if frac == "" and exp == "" then
        v = math.tointeger(tonumber(num))
        if v == nil then v = tonumber(num) end   -- too big for an integer -> float
    else
        v = tonumber(num)
    end
    if v == nil then fail(i, "bad number") end
    return v, i + #num
end

local function parseArray(s, i, depth)
    if depth > MAX_DECODE_DEPTH then fail(i, "too deep") end
    local arr, n = setmetatable({}, ARRAY), 0
    i = skipWs(s, i + 1)
    if s:sub(i, i) == "]" then return arr, i + 1 end
    while true do
        local v
        v, i = parseValue(s, i, depth + 1)
        n = n + 1
        if v == nil then arr[n] = NULL else arr[n] = v end
        i = skipWs(s, i)
        local c = s:sub(i, i)
        if c == "]" then return arr, i + 1 end
        if c ~= "," then fail(i, "expected , or ]") end
        i = skipWs(s, i + 1)
    end
end

local function parseObject(s, i, depth)
    if depth > MAX_DECODE_DEPTH then fail(i, "too deep") end
    local obj = {}
    i = skipWs(s, i + 1)
    if s:sub(i, i) == "}" then return obj, i + 1 end
    while true do
        if s:sub(i, i) ~= '"' then fail(i, "expected key string") end
        local k
        k, i = parseString(s, i)
        i = skipWs(s, i)
        if s:sub(i, i) ~= ":" then fail(i, "expected :") end
        i = skipWs(s, i + 1)
        local v
        v, i = parseValue(s, i, depth + 1)
        obj[k] = v           -- null -> nil: the key is left out
        i = skipWs(s, i)
        local c = s:sub(i, i)
        if c == "}" then return obj, i + 1 end
        if c ~= "," then fail(i, "expected , or }") end
        i = skipWs(s, i + 1)
    end
end

-- returns value (nil for null), next index
parseValue = function(s, i, depth)
    local c = s:sub(i, i)
    if c == "{" then return parseObject(s, i, depth)
    elseif c == "[" then return parseArray(s, i, depth)
    elseif c == '"' then return parseString(s, i)
    elseif c == "-" or c:match("^%d$") then return parseNumber(s, i)
    elseif s:sub(i, i + 3) == "true" then return true, i + 4
    elseif s:sub(i, i + 4) == "false" then return false, i + 5
    elseif s:sub(i, i + 3) == "null" then return nil, i + 4
    elseif c == "" then fail(i, "unexpected end")
    end
    fail(i, "unexpected character")
end

function json.decode(text)
    if type(text) ~= "string" then return nil, "not a string" end
    local ok, v, err = pcall(function()
        local s = text
        local start = 1
        if s:sub(1, 3) == "\239\187\191" then start = 4 end   -- UTF-8 BOM (Notepad)
        local i = skipWs(s, start)
        if i > #s then fail(i, "empty") end
        local val, j = parseValue(s, i, 0)
        j = skipWs(s, j)
        if j <= #s then fail(j, "extra text after the value") end
        if val == nil then return NULL end
        return val
    end)
    if ok then return v end
    if type(v) == "table" and v[FAIL] then return nil, v.msg end
    return nil, "json decode failed (" .. tostring(v) .. ")"
end

return json
