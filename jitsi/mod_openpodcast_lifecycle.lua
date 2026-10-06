-- Open Podcast: install on the Jitsi MUC component, NOT an anonymous host.
-- Depends on standard JWT token verification and token_affiliation for roles.
-- Reverse proxy this endpoint with HTTPS; restrict to the Open Podcast backend.
local json = require "util.json";
local st = require "util.stanza";
local muc = module:depends("muc");
module:depends("http");
local secret = assert(module:get_option_string("openpodcast_control_key"), "openpodcast_control_key required");
assert(#secret >= 32, "openpodcast_control_key must contain at least 32 characters");
local closed = module:open_store("openpodcast_closed", "keyval");

local function is_closed(jid)
    local expiry, err = closed:get(jid);
    if err then return true; end -- fail closed on storage failure
    return type(expiry) == "number" and expiry > os.time();
end

local function reject_closed(event)
    local room_jid = event.room and event.room.jid or event.stanza.attr.to:match("^[^/]+");
    if room_jid and is_closed(room_jid) then
        event.origin.send(st.error_reply(event.stanza, "cancel", "not-allowed", "Open Podcast session closed"));
        return true;
    end
end
-- Reject stale JWTs both when recreating a destroyed room and joining a live one.
module:hook("muc-room-pre-create", reject_closed, 100);
module:hook("muc-occupant-pre-join", reject_closed, 100);

module:provides("http", {
    default_path = "openpodcast";
    route = {
        ["POST /close"] = function(event)
            local request, response = event.request, event.response;
            response.headers.content_type = "application/json";
            if request.headers.authorization ~= "Bearer " .. secret then
                response.status_code = 403; return '{"closed":false}';
            end
            if not request.body or #request.body > 2048 then
                response.status_code = 413; return '{"closed":false}';
            end
            local data = json.decode(request.body);
            if type(data) ~= "table" or type(data.room) ~= "string"
                or not data.room:match("^op%-%x+$") or #data.room ~= 43
                or type(data.expires) ~= "number" or data.expires < os.time()
                or data.expires > os.time() + 600 then
                response.status_code = 400; return '{"closed":false}';
            end
            local jid = data.room .. "@" .. module.host;
            local ok = closed:set(jid, data.expires);
            if not ok then response.status_code = 503; return '{"closed":false}'; end
            local room = muc.get_room_from_jid(jid);
            if room then room:destroy(nil, "Open Podcast session closed"); end
            return '{"closed":true}';
        end;
    };
});
