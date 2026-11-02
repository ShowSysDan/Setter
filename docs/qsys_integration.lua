-- Setter / Q-SYS integration example
--
-- Paste into a Scripting (Text Controller) component. Create these controls
-- on it and the script wires them up:
--   Buttons (Trigger):  Cam1 .. Cam8, Cut, Auto, FTB
--   Text (Indicator):   Status
--
-- Every button sends one Setter control string over UDP. Replies arrive
-- on the same socket and land in the Status text box.

local SETTER_IP   = "10.1.1.50"   -- the machine running Setter
local SETTER_PORT = 9990          -- Settings > UDP control > Port
local FADE_RATE    = "1s"          -- frames ("25") or seconds ("1s")

local udp = UdpSocket.New()
udp:Open()                         -- any free local port; replies come back here

udp.Data = function(_, packet)
  local reply = packet.Data:gsub("%s+$", "")
  print("Setter: " .. reply)
  if Controls.Status then Controls.Status.String = reply end
end

local function send(cmd)
  udp:Send(SETTER_IP, SETTER_PORT, cmd .. "\n")
end

-- Camera buttons fade to that input
for n = 1, 8 do
  local btn = Controls["Cam" .. n]
  if btn then
    btn.EventHandler = function(ctl)
      if ctl.Boolean then send("FADE " .. n .. " " .. FADE_RATE) end
    end
  end
end

if Controls.Cut  then Controls.Cut.EventHandler  = function(c) if c.Boolean then send("CUT")  end end end
if Controls.Auto then Controls.Auto.EventHandler = function(c) if c.Boolean then send("AUTO") end end end
if Controls.FTB  then Controls.FTB.EventHandler  = function(c) if c.Boolean then send("FTB")  end end end

-- Keep the Status box current
local poll = Timer.New()
poll.EventHandler = function() send("STATUS") end
poll:Start(5)
send("STATUS")
