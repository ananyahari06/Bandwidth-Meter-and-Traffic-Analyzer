from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib import hub
import time


class BandwidthController(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(BandwidthController, self).__init__(*args, **kwargs)

        self.datapaths = {}
        self.failover_active = False

        # Store previous byte counters and timestamps
        # for interval-based throughput
        self.previous_bytes = {}
        self.previous_times = {}

        # Statistics polling interval
        self.poll_interval = 5

        # Start statistics monitoring thread
        self.monitor_thread = hub.spawn(self._monitor)

    def add_flow(self, datapath, priority, match, output_port):

        parser = datapath.ofproto_parser

        actions = [
            parser.OFPActionOutput(output_port)
        ]

        instructions = [
            parser.OFPInstructionActions(
                datapath.ofproto.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions
        )

        datapath.send_msg(flow_mod)

    def _monitor(self):

        while True:

            for datapath in self.datapaths.values():
                self._request_flow_stats(datapath)

            hub.sleep(self.poll_interval)

    def _request_flow_stats(self, datapath):

        parser = datapath.ofproto_parser

        request = parser.OFPFlowStatsRequest(
            datapath
        )

        datapath.send_msg(request)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):

        datapath = ev.msg.datapath
        dpid = datapath.id

        print("")
        print("========================================")
        print(f"FLOW STATISTICS - SWITCH s{dpid}")
        print("========================================")

        for stat in ev.msg.body:

            # Ignore table-miss flow
            if stat.priority == 0:
                continue

            duration = (
                stat.duration_sec +
                stat.duration_nsec / 1000000000
            )

            output_port = "unknown"

            for instruction in stat.instructions:

                if hasattr(instruction, "actions"):

                    for action in instruction.actions:

                        if hasattr(action, "port"):
                            output_port = action.port

            match_fields = []

            if "ipv4_dst" in stat.match:
                match_fields.append(
                    f"dst={stat.match['ipv4_dst']}"
                )

            if "arp_tpa" in stat.match:
                match_fields.append(
                    f"arp_tpa={stat.match['arp_tpa']}"
                )

            if "arp_spa" in stat.match:
                match_fields.append(
                    f"arp_spa={stat.match['arp_spa']}"
                )

            match_text = ", ".join(match_fields)

            # Unique identifier for this flow
            flow_key = (
                dpid,
                stat.priority,
                match_text,
                output_port
            )

            current_bytes = stat.byte_count
            current_time = time.monotonic()

            # Calculate throughput using actual elapsed time
            if flow_key in self.previous_bytes:

                previous_bytes = self.previous_bytes[flow_key]
                previous_time = self.previous_times[flow_key]

                byte_difference = current_bytes - previous_bytes
                time_difference = current_time - previous_time

                # Handle counter reset or invalid time difference
                if byte_difference < 0 or time_difference <= 0:

                    throughput_kbps = 0.0

                else:

                    throughput_bps = (
                        byte_difference * 8
                    ) / time_difference

                    throughput_kbps = throughput_bps / 1000

            else:

                # First reading has no previous value
                throughput_kbps = 0.0

            # Store current values for next measurement
            self.previous_bytes[flow_key] = current_bytes
            self.previous_times[flow_key] = current_time

            print(f"Priority       : {stat.priority}")
            print(f"Match          : {match_text}")
            print(f"Packets        : {stat.packet_count}")
            print(f"Bytes          : {stat.byte_count}")
            print(f"Duration       : {duration:.2f} seconds")
            print(f"Output port    : {output_port}")

            # Map switch ports to connected devices
            if dpid == 1:

                port_names = {
                    1: "h1",
                    2: "s2",
                    3: "s3"
                }

            elif dpid == 2:

                port_names = {
                    1: "s1",
                    2: "s4"
                }

            elif dpid == 3:

                port_names = {
                    1: "s1",
                    2: "s5"
                }

            elif dpid == 4:

                port_names = {
                    1: "s2",
                    2: "s6"
                }

            elif dpid == 5:

                port_names = {
                    1: "s3",
                    2: "s6"
                }

            elif dpid == 6:

                port_names = {
                    1: "h2",
                    2: "h3",
                    3: "s4",
                    4: "s5"
                }

            else:

                port_names = {}

            output_name = port_names.get(
                output_port,
                "unknown"
            )

            print(f"Output device  : {output_name}")
            print(
                f"SDN throughput  : {throughput_kbps:.2f} Kbps"
            )

            print("----------------------------------------")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):

        datapath = ev.msg.datapath
        dpid = datapath.id

        self.datapaths[dpid] = datapath

        print(f"Switch connected: {dpid}")

        parser = datapath.ofproto_parser

        # Primary path from h1 to h2
        h2_path = {
            1: 2,
            2: 2,
            4: 2,
            6: 1
        }

        # Primary path from h1 to h3
        h3_path = {
            1: 3,
            3: 2,
            5: 2,
            6: 2
        }

        # Return path from h2 to h1
        h2_return = {
            6: 3,
            4: 1,
            2: 1,
            1: 1
        }

        # Return path from h3 to h1
        h3_return = {
            6: 4,
            5: 1,
            3: 1,
            1: 1
        }

        # Backup path for h1 to h2
        h2_backup = {
            3: 2,
            5: 2
        }

        # Backup return path for h2 to h1
        h2_backup_return = {
            5: 1,
            3: 1
        }

        # -------------------------------
        # IPv4 forwarding rules
        # -------------------------------

        if dpid in h2_path:

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_dst="10.0.0.2"
            )

            self.add_flow(
                datapath,
                10,
                match,
                h2_path[dpid]
            )

        if dpid in h3_path:

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_dst="10.0.0.3"
            )

            self.add_flow(
                datapath,
                10,
                match,
                h3_path[dpid]
            )

        if dpid in h2_return:

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_dst="10.0.0.1"
            )

            self.add_flow(
                datapath,
                10,
                match,
                h2_return[dpid]
            )

        if dpid in h3_return:

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_dst="10.0.0.1"
            )

            self.add_flow(
                datapath,
                10,
                match,
                h3_return[dpid]
            )

        if dpid in h2_backup:

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_dst="10.0.0.2"
            )

            self.add_flow(
                datapath,
                10,
                match,
                h2_backup[dpid]
            )

        if dpid in h2_backup_return:

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_dst="10.0.0.1"
            )

            self.add_flow(
                datapath,
                10,
                match,
                h2_backup_return[dpid]
            )

        # -------------------------------
        # ARP forwarding rules
        # -------------------------------

        if dpid in h2_path:

            match = parser.OFPMatch(
                eth_type=0x0806,
                arp_tpa="10.0.0.2"
            )

            self.add_flow(
                datapath,
                5,
                match,
                h2_path[dpid]
            )

        if dpid in h2_return:

            match = parser.OFPMatch(
                eth_type=0x0806,
                arp_spa="10.0.0.2"
            )

            self.add_flow(
                datapath,
                5,
                match,
                h2_return[dpid]
            )

        if dpid in h3_path:

            match = parser.OFPMatch(
                eth_type=0x0806,
                arp_tpa="10.0.0.3"
            )

            self.add_flow(
                datapath,
                5,
                match,
                h3_path[dpid]
            )

        if dpid in h3_return:

            match = parser.OFPMatch(
                eth_type=0x0806,
                arp_spa="10.0.0.3"
            )

            self.add_flow(
                datapath,
                5,
                match,
                h3_return[dpid]
            )

        if dpid in h2_backup:

            match = parser.OFPMatch(
                eth_type=0x0806,
                arp_tpa="10.0.0.2"
            )

            self.add_flow(
                datapath,
                5,
                match,
                h2_backup[dpid]
            )

        if dpid in h2_backup_return:

            match = parser.OFPMatch(
                eth_type=0x0806,
                arp_spa="10.0.0.2"
            )

            self.add_flow(
                datapath,
                5,
                match,
                h2_backup_return[dpid]
            )

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):

        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        port = msg.desc
        ofproto = datapath.ofproto

        # Monitor s1 port 2 = s1-s2 link
        if dpid != 1 or port.port_no != 2:
            return

        link_failed = False

        if msg.reason == ofproto.OFPPR_DELETE:

            link_failed = True

        elif msg.reason == ofproto.OFPPR_MODIFY:

            if port.state & ofproto.OFPPS_LINK_DOWN:
                link_failed = True

        if not link_failed:
            return

        # Avoid activating failover multiple times
        if self.failover_active:
            return

        self.failover_active = True

        print("")
        print("========================================")
        print("FAILURE DETECTED")
        print("s1 -> s2 link has failed")
        print("========================================")
        print("")

        # -------------------------------
        # Redirect h1 -> h2 through s3
        # -------------------------------

        s1 = self.datapaths.get(1)

        if s1 is not None:

            parser = s1.ofproto_parser

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_dst="10.0.0.2"
            )

            self.add_flow(
                s1,
                20,
                match,
                3
            )

            match_arp = parser.OFPMatch(
                eth_type=0x0806,
                arp_tpa="10.0.0.2"
            )

            self.add_flow(
                s1,
                20,
                match_arp,
                3
            )

            print("s1: h1 -> h2 redirected to s3")

        # -------------------------------
        # Redirect h2 -> h1 through s5
        # -------------------------------

        s6 = self.datapaths.get(6)

        if s6 is not None:

            parser = s6.ofproto_parser

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_dst="10.0.0.1"
            )

            self.add_flow(
                s6,
                20,
                match,
                4
            )

            match_arp = parser.OFPMatch(
                eth_type=0x0806,
                arp_spa="10.0.0.2"
            )

            self.add_flow(
                s6,
                20,
                match_arp,
                4
            )

            print("s6: h2 -> h1 return traffic redirected to s5")

        print("")
        print("========================================")
        print("FAILOVER ACTIVE")
        print("Backup path:")
        print("h1 -> s1 -> s3 -> s5 -> s6 -> h2")
        print("h2 -> s6 -> s5 -> s3 -> s1 -> h1")
        print("========================================")
        print("")
