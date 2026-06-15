import asyncio
import plugins.libmesh as LibMesh
import discord

def _coerce_node_num(value):
    if isinstance(value, int):
        return value

    if isinstance(value, str):
        if value.startswith("!"):
            try:
                return int(value[1:], 16)
            except ValueError:
                return None
        try:
            return int(value)
        except ValueError:
            return None

    return None

def _get_node_num(node_key, node_info):
    candidates = [
        node_key,
        node_info.get("num"),
        node_info.get("nodeNum"),
        node_info.get("from"),
        node_info.get("user", {}).get("id"),
    ]

    for candidate in candidates:
        node_num = _coerce_node_num(candidate)
        if node_num is not None:
            return node_num

    return None

def resolveRelayNode(interface, packet):
    relay_node = packet.get("relayNode")
    if relay_node is None:
        return None

    relay_num = _coerce_node_num(relay_node)
    if relay_num is None:
        relay_id = str(relay_node)
        return {"id": relay_id, "name": relay_id}

    matches = []
    source_id = packet.get("fromId")
    for node_key, node_info in getattr(interface, "nodes", {}).items():
        node_id = node_info.get("user", {}).get("id")
        if node_id == source_id:
            continue

        node_num = _get_node_num(node_key, node_info)
        if node_num is None or (node_num & 0xFF) != (relay_num & 0xFF):
            continue

        user = node_info.get("user", {})
        node_name = user.get("longName") or user.get("shortName") or node_id or f"!{node_num:08x}"
        matches.append({
            "id": node_id or f"!{node_num:08x}",
            "name": node_name,
            "last_heard": node_info.get("lastHeard", 0),
            "snr": node_info.get("snr", -999),
        })

    if matches:
        matches.sort(key=lambda node: (node["last_heard"], node["snr"]), reverse=True)
        return matches[0]

    relay_id = f"!{relay_num:08x}" if relay_num > 0xFF else f"0x{relay_num & 0xFF:02x}"
    return {"id": relay_id, "name": relay_id}

def formatRelayNode(interface, packet):
    relay = resolveRelayNode(interface, packet)
    if not relay:
        return None

    relay_name = relay.get("name")
    relay_id = relay.get("id")
    if relay_name and relay_id and relay_name != relay_id:
        return f"{relay_name} ({relay_id})"
    return relay_name or relay_id

def genUserName(interface, packet, details=True):
    short = LibMesh.getUserShort(interface, packet)
    long  = LibMesh.getUserLong(interface, packet) or ""
    lat, lon, hasPos = LibMesh.getPosition(interface, packet)

    ret = f"**{long}** \n _(Short: {short})_ " if short is not None else " \n"

    #ret += f"Short: ({short}) " if short is not None else " "

    if details:
        if packet.get("fromId") is not None:
            ret += f"_ID: {packet['fromId']}_ \n"

    if details and hasPos:
        ret += f" [map](<https://www.google.com/maps/search/?api=1&query={lat}%2C{lon}>) "

    if "hopLimit" in packet:
        if "hopStart" in packet:
            ret += f"🐇 {packet['hopStart'] - packet['hopLimit']} of {packet['hopStart']} \n"
        else:
            ret += f"🐇 {packet['hopLimit']} \n"

    if "viaMqtt" in packet and str(packet["viaMqtt"]) == "True":
        ret += " `MQTT`"

    relay_display = formatRelayNode(interface, packet)
    if details and relay_display:
        ret += f"Last hop: {relay_display}\n"

    return ret

def send_msg(message,client,config,channel_id=0):
    if config["use_discord"]:
        if (client.is_ready()):
            if config.get("secondary_channel_message_ids") and channel_id and channel_id > 0:
                chan = config["secondary_channel_message_ids"][channel_id-1]
                asyncio.run_coroutine_threadsafe(client.get_channel(chan).send(message),client.loop)
            else:
                for i in config["message_channel_ids"]:
                    asyncio.run_coroutine_threadsafe(client.get_channel(i).send(message),client.loop)

def send_embed(title, description, client, config, channel_id=0, footer=None, color=0x3c90ba):
    if config["use_discord"]:
        if (client.is_ready()):
            embed = discord.Embed(title=title, description=description, color=color)
            if footer:
                embed.set_footer(text=footer)
            channels = []
            if config.get("secondary_channel_message_ids") and channel_id and channel_id > 0:
                channels.append(config["secondary_channel_message_ids"][channel_id-1])
            else:
                channels = config["message_channel_ids"]
            for chan_id in channels:
                asyncio.run_coroutine_threadsafe(client.get_channel(chan_id).send(embed=embed), client.loop)

def send_info(message,client,config):
    if config["use_discord"]:
        if (client.is_ready()):
            for i in config["info_channel_ids"]:
                asyncio.run_coroutine_threadsafe(client.get_channel(i).send(message),client.loop)
