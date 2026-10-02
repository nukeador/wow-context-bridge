-- WoWContextBridge: local, experimental context and nameplate vocabulary output.
-- No game input, network access, memory access, or persistent logging.

local MAGIC1, MAGIC2 = 0xD3, 0x71
local VERSION = 2
local MAX_PAYLOAD = 256
local COLUMNS = 128
local CELL = 3
local HEARTBEAT_SECONDS = 3
local MAX_NAMEPLATE_TOKENS = 40
local MAX_NEARBY_NAMES = 8
local pendingUpdate = false
local updateElapsed = 0

local function checksum(bytes)
    local s1, s2 = 0, 0
    for i = 1, #bytes do
        s1 = (s1 + string.byte(bytes, i)) % 255
        s2 = (s2 + s1) % 255
    end
    return string.char(s1, s2)
end

local function utf8Prefix(value, limit)
    local n = math.min(#value, limit)
    if n == #value then return value end
    local start = n
    while start > 0 do
        local b = string.byte(value, start)
        if b < 128 or b >= 192 then break end
        start = start - 1
    end
    if start > 0 then
        local lead = string.byte(value, start)
        local need = 1
        if lead >= 240 then need = 4
        elseif lead >= 224 then need = 3
        elseif lead >= 192 then need = 2 end
        if start + need - 1 > n then n = start - 1 end
    end
    return string.sub(value, 1, n)
end

local function dropLastCodepoint(value)
    local i = #value
    while i > 0 do
        local b = string.byte(value, i)
        if b < 128 or b >= 192 then break end
        i = i - 1
    end
    if i == 0 then return "" end
    return string.sub(value, 1, i - 1)
end

local function gameText(value)
    if type(value) ~= "string" then return "" end
    -- Display names should not contain controls; normalize any that do.
    value = string.gsub(value, "[%z\1-\31]", " ")
    return utf8Prefix(value, 96)
end

-- Retail can restrict some context values in protected situations. Keep those
-- values local to the client and fail closed if reading or sanitizing one fails.
local readFailures = 0
local function safeGameText(api, ...)
    if type(api) ~= "function" then
        readFailures = readFailures + 1
        return ""
    end
    local ok, value = pcall(api, ...)
    if not ok then
        readFailures = readFailures + 1
        return ""
    end
    if type(issecretvalue) == "function" then
        local secretOK, secret = pcall(issecretvalue, value)
        if not secretOK or secret then
            readFailures = readFailures + 1
            return ""
        end
    end
    local textOK, text = pcall(gameText, value)
    if not textOK then
        readFailures = readFailures + 1
        return ""
    end
    return text
end

local function quoteJson(value)
    local out = { '"' }
    for i = 1, #value do
        local b = string.byte(value, i)
        if b == 34 then out[#out + 1] = string.char(92, 34)
        elseif b == 92 then out[#out + 1] = string.char(92, 92)
        elseif b < 32 then out[#out + 1] = string.format('\\u%04x', b)
        else out[#out + 1] = string.char(b) end
    end
    out[#out + 1] = '"'
    return table.concat(out)
end

local function nearbyNames()
    local names, seen = {}, {}
    -- Bounded standard nameplate tokens; no world/object enumeration.
    -- Fresh reads each time ensure removed plates do not remain cached.
    for i = 1, MAX_NAMEPLATE_TOKENS do
        local name = safeGameText(UnitName, "nameplate" .. i)
        if name ~= "" and not seen[name] then
            seen[name] = true
            names[#names + 1] = name
        end
    end
    table.sort(names)
    return names
end

local function makePayload(fields)
    local names = nearbyNames()
    fields.nearby = {}
    fields.nearby_total = #names
    local function serialize()
        local quoted = {}
        for i, name in ipairs(fields.nearby) do quoted[i] = quoteJson(name) end
        return '{"player":' .. quoteJson(fields.player)
            .. ',"zone":' .. quoteJson(fields.zone)
            .. ',"subzone":' .. quoteJson(fields.subzone)
            .. ',"target":' .. quoteJson(fields.target)
            .. ',"nearby":[' .. table.concat(quoted, ',') .. ']'
            .. ',"nearby_total":' .. fields.nearby_total .. '}'
    end
    local payload = serialize()
    while #payload > MAX_PAYLOAD do
        local longest = nil
        for _, key in ipairs({ "player", "zone", "subzone", "target" }) do
            if not longest or #fields[key] > #fields[longest] then longest = key end
        end
        if not longest or #fields[longest] == 0 then break end
        fields[longest] = dropLastCodepoint(fields[longest])
        payload = serialize()
    end
    for _, name in ipairs(names) do
        if #fields.nearby >= MAX_NEARBY_NAMES then break end
        fields.nearby[#fields.nearby + 1] = name
        local candidate = serialize()
        if #candidate <= MAX_PAYLOAD then payload = candidate
        else fields.nearby[#fields.nearby] = nil end
    end
    return payload
end

local function bytesToCells(bytes)
    local cells, acc, nbits = {}, 0, 0
    for i = 1, #bytes do
        acc = acc * 256 + string.byte(bytes, i)
        nbits = nbits + 8
        while nbits >= 3 do
            local shift = nbits - 3
            cells[#cells + 1] = math.floor(acc / (2 ^ shift)) % 8
            nbits = shift
            acc = acc % (2 ^ nbits)
        end
    end
    if nbits > 0 then cells[#cells + 1] = (acc * (2 ^ (3 - nbits))) % 8 end
    return cells
end

local function makePacket(sequence, payload)
    local body = string.char(VERSION, math.floor(sequence / 256), sequence % 256,
        math.floor(#payload / 256), #payload % 256) .. payload
    return string.char(MAGIC1, MAGIC2) .. body .. checksum(body)
end

local frame = CreateFrame("Frame", "WoWContextBridgeFrame", UIParent)
frame:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)
frame:SetFrameStrata("TOOLTIP")
frame:SetFrameLevel(100)
local screenHeight
if type(GetPhysicalScreenSize) == "function" then
    local _, height = GetPhysicalScreenSize()
    screenHeight = height
elseif type(GetScreenHeight) == "function" then
    screenHeight = GetScreenHeight()
end
if type(screenHeight) == "number" and screenHeight > 0 then
    frame:SetScale(768 / screenHeight)
end

local textures = {}
local enabled = true
local sequence = 0
local lastPayload = nil
local heartbeatElapsed = 0
local changes = 0
local heartbeats = 0
local lastFields = { player = "", zone = "", subzone = "", target = "" }

local function setCell(index, value)
    local texture = textures[index]
    if not texture then
        texture = frame:CreateTexture(nil, "ARTWORK")
        textures[index] = texture
    end
    local cellIndex = index - 1
    local col = cellIndex % COLUMNS
    local row = math.floor(cellIndex / COLUMNS)
    texture:ClearAllPoints()
    texture:SetPoint("TOPLEFT", frame, "TOPLEFT", col * CELL, -row * CELL)
    texture:SetSize(CELL, CELL)
    local r = math.floor(value / 4) % 2
    local g = math.floor(value / 2) % 2
    local b = value % 2
    if texture.SetColorTexture then texture:SetColorTexture(r, g, b, 1)
    else
        texture:SetTexture("Interface\\Buttons\\WHITE8X8")
        texture:SetVertexColor(r, g, b, 1)
    end
    texture:Show()
end

local function hideExtraCells(count)
    for i = count + 1, #textures do
        textures[i]:Hide()
    end
end

local function publish(isHeartbeat)
    local player = safeGameText(UnitName, "player")
    local zone = safeGameText(GetZoneText)
    local subzone = safeGameText(GetSubZoneText)
    local target = safeGameText(UnitName, "target")
    local fields = { player = player, zone = zone, subzone = subzone, target = target }
    local payload = makePayload(fields)
    lastFields = fields
    if not enabled then return end
    if isHeartbeat or payload ~= lastPayload then
        sequence = (sequence + 1) % 65536
        if isHeartbeat then heartbeats = heartbeats + 1 else changes = changes + 1 end
        local cells = bytesToCells(makePacket(sequence, payload))
        for i = 1, #cells do setCell(i, cells[i]) end
        hideExtraCells(#cells)
        frame:SetSize(COLUMNS * CELL, math.ceil(#cells / COLUMNS) * CELL)
        frame:Show()
        lastPayload = payload
    end
end

local events = {
    "PLAYER_ENTERING_WORLD",
    "ZONE_CHANGED",
    "ZONE_CHANGED_INDOORS",
    "ZONE_CHANGED_NEW_AREA",
    "PLAYER_TARGET_CHANGED",
    "NAME_PLATE_UNIT_ADDED",
    "NAME_PLATE_UNIT_REMOVED",
    "UNIT_NAME_UPDATE",
}
for _, eventName in ipairs(events) do frame:RegisterEvent(eventName) end
frame:SetScript("OnEvent", function()
    pendingUpdate = true
end)
frame:SetScript("OnUpdate", function(_, elapsed)
    heartbeatElapsed = heartbeatElapsed + elapsed
    updateElapsed = updateElapsed + elapsed
    if pendingUpdate and updateElapsed >= 0.25 then
        pendingUpdate = false
        updateElapsed = 0
        heartbeatElapsed = 0
        publish(false)
    elseif heartbeatElapsed >= HEARTBEAT_SECONDS then
        heartbeatElapsed = heartbeatElapsed % HEARTBEAT_SECONDS
        publish(true)
    end
end)
publish(false)

SLASH_WOWCONTEXTBRIDGE1 = "/wcb"
SlashCmdList.WOWCONTEXTBRIDGE = function(message)
    local command = string.lower(message or "")
    if command == "off" then
        enabled = false
        frame:Hide()
        print("[WoWContextBridge] pixel output disabled")
    elseif command == "on" then
        enabled = true
        lastPayload = nil
        publish(false)
        print("[WoWContextBridge] pixel output enabled")
    elseif command == "diag" or command == "status" or command == "" then
        print(string.format(
            "[WoWContextBridge] enabled=%s seq=%d payload=%dB cells=%d changes=%d heartbeats=%d readFailures=%d nearby=%d/%d player=%s zone=%s subzone=%s target=%s",
            tostring(enabled), sequence, lastPayload and #lastPayload or 0, #textures,
            changes, heartbeats, readFailures, #(lastFields.nearby or {}),
            lastFields.nearby_total or 0, lastFields.player, lastFields.zone,
            lastFields.subzone, lastFields.target))
    else
        print("[WoWContextBridge] use /wcb diag, /wcb on or /wcb off")
    end
end
