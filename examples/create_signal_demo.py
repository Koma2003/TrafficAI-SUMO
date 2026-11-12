"""Build a reproducible four-arm vehicle-only intersection with netconvert."""
from pathlib import Path
import subprocess


def main():
    target = Path(__file__).resolve().parent / 'signal_demo'
    target.mkdir(parents=True, exist_ok=True)
    (target/'nodes.nod.xml').write_text('''<nodes>
    <node id="center" x="0" y="0" type="traffic_light"/>
    <node id="north" x="0" y="250"/><node id="south" x="0" y="-250"/>
    <node id="east" x="250" y="0"/><node id="west" x="-250" y="0"/></nodes>''',encoding='utf-8')
    edges=['<edges>']
    for arm in ['north','south','east','west']:
        edges.extend([f'<edge id="{arm}_in" from="{arm}" to="center" numLanes="1" speed="13.89"/>',
                      f'<edge id="{arm}_out" from="center" to="{arm}" numLanes="1" speed="13.89"/>'])
    (target/'edges.edg.xml').write_text('\n'.join(edges+['</edges>']),encoding='utf-8')
    pairs=[('north','south'),('south','north'),('east','west'),('west','east')]
    (target/'connections.con.xml').write_text('<connections>'+''.join(f'<connection from="{a}_in" to="{b}_out" fromLane="0" toLane="0"/>' for a,b in pairs)+'</connections>',encoding='utf-8')
    subprocess.run(['netconvert','--node-files',str(target/'nodes.nod.xml'),'--edge-files',str(target/'edges.edg.xml'),
                    '--connection-files',str(target/'connections.con.xml'),'--output-file',str(target/'demo.net.xml'),
                    '--tls.green.time','30','--tls.yellow.time','3','--no-turnarounds','true'],check=True)
    routes=['<routes><vType id="car" vClass="passenger"/>']
    for a,b in pairs:
        rate=650 if a in ['north','south'] else 300
        routes.append(f'<flow id="{a}" type="car" begin="0" end="360" vehsPerHour="{rate}"><route edges="{a}_in {b}_out"/></flow>')
    (target/'demo.rou.xml').write_text('\n'.join(routes+['</routes>']),encoding='utf-8')
    (target/'demo.sumocfg').write_text('''<configuration><input><net-file value="demo.net.xml"/>
    <route-files value="demo.rou.xml"/></input><time><begin value="0"/><end value="360"/></time>
    <processing><time-to-teleport value="-1"/></processing></configuration>''',encoding='utf-8')
    print('SUMO demo:',target)


if __name__=='__main__': main()
