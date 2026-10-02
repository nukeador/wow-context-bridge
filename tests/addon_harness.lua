-- Run with Lua 5.1; exercises the real addon encoder through mocked WoW UI APIs.
local scripts, textures = {}, {}
UIParent = {}; SlashCmdList = {}
local noop = function() end
local frame = setmetatable({}, {__index=function() return noop end})
function frame:SetScript(name, fn) scripts[name] = fn end
function frame:CreateTexture()
    local t = setmetatable({}, {__index=function() return noop end})
    function t:SetColorTexture(r,g,b) self.value=r*4+g*2+b; self.hidden=false end
    function t:Hide() self.hidden=true end
    textures[#textures+1]=t; return t
end
function CreateFrame() return frame end
function GetPhysicalScreenSize() return 1280,800 end
function GetZoneText() return "Zone" end
function GetSubZoneText() return "Subzone" end
local names = {"Álvaro", "Мария", "Álvaro", "SECRET", 'A"\\B'}
function issecretvalue(v) return v == "SECRET" end
function UnitName(unit)
    if unit == "player" then return "Player" end
    if unit == "target" then return "Target" end
    local n=tonumber(unit:match("^nameplate(%d+)$"))
    return n and names[n] or nil
end
local function output()
    local cells={}
    for _,t in ipairs(textures) do if not t.hidden then cells[#cells+1]=tostring(t.value) end end
    io.write("["..table.concat(cells,",").."]\n")
end
dofile("addon/WoWContextBridge/WoWContextBridge.lua")
output()
names={}; scripts.OnEvent(frame,"NAME_PLATE_UNIT_REMOVED","nameplate1");scripts.OnUpdate(frame,0.25);output()
for i=1,40 do names[i]="NPC"..i..string.rep("界",20) end
scripts.OnEvent(frame,"NAME_PLATE_UNIT_ADDED","nameplate1");scripts.OnUpdate(frame,0.25);output()
