import asyncio
import logging
import re
import argparse

from aiohttp import web
from aiorcon import RCON
from jinja2 import Template

# Logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# If possible, try to use uvloop for better performance
try:
    import uvloop
    uvloop.install()
    logger.info("Using uvloop")
except Exception:
    pass

# Statistic Mapper
STATS_MAPPING = {
    "In": "NetIn",
    "In_(KB/s)": "NetIn",
    "Out": "NetOut",
    "Out_(KB/s)": "NetOut",
    "+-ms": "varms",
    "~tick": "vartick",
    "Svms": "svarms",
    "Map_changes": "Maps",
}

TEMPLATE_FILE = "response.j2"


class TargetSpecificationError(Exception):
    pass


class SRCDSExporter:

    def __init__(self, ip=None, port=None, password=None, single_server=False):

        self._ip = ip
        self._port = port
        self._password = password
        self._single_server = single_server
        # TODO add check that ip, port and password are correct values if
        # single_server is True

    async def request_handler(self, request):
        """
        Handler method for aiohttp. Called for each HTTP request.
        :param request: HTTPRequest Object as given by aiohttp
        """
        if request.path != "/metrics":
            return web.Response(text="invalid path", status=404)

        # Find the exact target specification
        try:
            ip, port, password = self._get_target(request)
        except TargetSpecificationError as e:
            # TODO improve log message
            logger.info("received invalid target specification:%s" % str(e))
            return web.Response(text="target specification is invalid: %s"
                                % str(e), status=404)

        # make the RCON queries
        try:
            status, stats = await self._rcon_query(ip, port, password)
        except Exception as e:
            if isinstance(e, TimeoutError):
                logger.info("a timeout error occured during the RCON request.")
                return self._server_down_response()
            if isinstance(e, ConnectionRefusedError):
                logger.info("Connection was refused by the gameserver")
                return web.Response(text="Connection refused by target",
                                    status=503)  # TODO improve log message
            # Add other Exception types here
            # TODO improve log message
            logger.warning("An Exception occured during the following "
                           "request:%s" % str(request))
            logger.exception(e)
            return self._server_down_response()

        server_dict = {
            "ip": ip,
            "port": port,
            "target": "{%s}:{%s}" % (ip, port)
        }

        self._parse_query(stats, status, server_dict)
        return web.Response(text=template.render(**server_dict))

    def _server_down_response(self):
        """
        This method returns a response with only the srcds_ip metric as 0
        """
        resp = ("# HELP srcds_up is the gameserver reachable\n"
                "# TYPE srcds_up gauge\n"
                "srcds_up 0")

        return web.Response(text=resp)

    async def _rcon_query(self, ip, port, password):
        """
        queries the server given by ip and port with the stats and status
        commands and returns the answer.
        """
        # this first wait_for is to work around something which is maybe
        # a bug in aiorcon
        status = None
        stats = None
        rcon = None
        try:
            rcon = await asyncio.wait_for(
                    RCON.create(ip,
                                port,
                                password,
                                # the loop is required due to a
                                # bug in aiorcon. See
                                # https://github.com/skmendez/aiorcon/pull/1
                                loop=asyncio.get_event_loop(),
                                timeout=1,
                                auto_reconnect_attempts=2),
                    2)
            if rcon is None:
                raise Error
            print(rcon)

            status = await asyncio.wait_for(rcon("status"), 2)
            stats = await asyncio.wait_for(rcon("stats"), 2)
        finally:
            if rcon is not None:
                rcon.close()
        return status, stats

    def _get_target(self, request):
        """
        returns a tuple of (ip, port, password) which to query depending
        on the data given in the Exporter and the request.
        Raises an exception if the target specification is invalid.

        :param aiohttp.Request request: The request received by the webserver
        :returns tuple(str, str, str): ip, port and password
        """
        if self._single_server:
            # The server is running in single server mode,
            # target specifications are invalid in this mode
            for key in ["target", "password"]:
                if key in request.query:
                    raise TargetSpecificationError(
                            "'target' and 'password' not"
                            " allowed in single server mode.")

            return (self._ip, self._port, self._password)

        else:
            try:
                target = request.query["target"]
                targets = target.split(":")
                ip = targets[0]
                port = targets[1]
            except Exception:
                raise TargetSpecificationError(
                        "target %s is not a valid target"
                        "specification" % target)
            try:
                password = request.query["password"]
            except Exception:
                raise TargetSpecificationError("no password given")

            return ip, port, password

    def _parse_query(self, stats, status, server_dict):
        """
        Parses the responses to *stats* and *status* from the gameserver
        and adds the parameters to the *server_dict*.

        :param stats: str, the output of the *stats* command
        :param status: str, the output of the *status* command
        :param server_dict: dictionary with metric names and their values
        """

        self._parse_status(status, server_dict)
        self._parse_stats(stats, server_dict)

    def _parse_stats(self, stats, server_dict):
        """
        Parses the status RCON response and adds the data to *server_dict*

        :param str stats: the output of the *stats* command
        :param dict server_dict: the dict to which the data is added
        """
        lines = [a.split() for a in stats.splitlines()]
        names = lines[0]
        values = lines[1]

        # Replace stats
        for i, name in enumerate(names):
            if name in STATS_MAPPING.keys():
                names[i] = STATS_MAPPING[name]

        values = [float(v) for v in values]

        for key, value in zip(names, values):
            server_dict[key] = value

    def _parse_status(self, status, server_dict):
        """
        Parses the status RCON response and adds the data to *server_dict*

        :param str status: the output of the *status* command
        :param dict server_dict: the dict to which the data is added
        """
        status = status.splitlines()
        for line in status:
            # break at the empty line between the key-value pairs and the
            # players
            if line.strip() == "":
                break
            try:
                key, value = (a.strip() for a in line.split(":", 1))
            except ValueError:
                # FoF has no blank line between the key value
                # pairs and the player list. This results in a
                # ValueError.
                break

            if key == "players":
                # Regex explanation:
                # There are 3 types of players-patterns
                # "0 humans, 0 bots (16/0 max) (hibernating)" # CSGO
                # "0 (16 max)" # Gmod
                # "12 humans, 0 bots (16 max)" # everything else
                # The first number is always the amount of players, followed
                # by a space. Then there might be the string "humans, "
                # but not for Gmod. Then comes the number of bots, but
                # not for Gmod and than the max_players in brackets.
                # csgo has some special things here where the have a 16/0
                # I currently don't know what the /0 stands for. Maybe STV
                # viewers?

                m = re.match(
                    r"(?P<players>\d+)\s+(humans,\s+)?"
                    r"((?P<bots>\d+)\s+bots\s+)?"
                    r"\((?P<max_players>\d+)(/\d)? max\)",
                    value
                )

                if m:
                    for key, value in m.groupdict().items():
                        server_dict[key] = value

            elif key == "hostname":  # Hostname
                server_dict["hostname"] = value


# Parse response file
with open("response.j2", "r") as f:
    t = f.read()
    template = Template(t, trim_blocks=True, lstrip_blocks=True,)


async def start_webserver(loop, args):
    """
    Starts up the server

    :param asyncio.AbstractEventLoop loop: the asyncio event loop to use
    :param argsparse.Namespace args: the namespace given by argparse
    """

    if args.server_address and args.server_port and args.password:
        exporter = SRCDSExporter(args.server_address,
                                 args.server_port,
                                 args.password,
                                 True)
    else:  # Not in single server mode
        exporter = SRCDSExporter(None, None, None)  # TODO add data from parser

    server = web.Server(exporter.request_handler)

    await loop.create_server(server, args.address, args.port)


if __name__ == "__main__":
    """Builds the argument parser and starts a server in a asyncio-loop"""
    argparser = argparse.ArgumentParser(description=(
            "srcds_exporter, an prometheus exporter for SRCDS based games "
            "like CSGO, L4D2 and TF2"))
    argparser.add_argument("--port", type=int, default=9591,
                           help="the port to which the exporter binds")
    argparser.add_argument("--address", type=str, default="127.0.0.1",
                           help="the address to which the exporter binds")
    argparser.add_argument("--password", type=str, default=None,
                           help="the password that is used if the exporter "
                                "is run in single server mode")
    argparser.add_argument("--server_address", type=str, default="localhost",
                           help="the address which is queried if the is "
                                "exporter is run in single server mode")
    argparser.add_argument("--server_port", type=int, default=27015,
                           help="the queried which is queried if the is "
                                "exporter is run in single server mode")
    args = argparser.parse_args()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_webserver(loop, args))
    loop.run_forever()
