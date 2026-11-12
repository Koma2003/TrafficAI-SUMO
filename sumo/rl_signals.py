"""Experimental tabular Q-learning for one vehicle-only SUMO intersection.

Learns bounded green/yellow/all-red durations. Existing conflict-free green
states are preserved. Pedestrian networks are rejected by this demo controller.
"""
import argparse
import html
import json
import math
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import traci


def duration_choices(minimum, maximum, step):
    if not all(math.isfinite(v) for v in (minimum, maximum, step)) or minimum <= 0 or maximum < minimum or step <= 0:
        raise ValueError('Требуется 0 < минимум <= максимум и положительный шаг')
    count = int((maximum-minimum)/step)
    if count > 50:
        raise ValueError('Слишком много вариантов: увеличьте шаг длительности')
    return sorted(set([minimum + i*step for i in range(count+1)] + [maximum]))


def load_program(config):
    config = Path(config).resolve()
    root = ET.parse(config).getroot()
    entry = root.find('input/net-file')
    if entry is None: raise ValueError('В конфиге нет net-file')
    net = (config.parent / entry.get('value')).resolve()
    network = ET.parse(net).getroot()
    if any(e.get('function') in {'crossing', 'walkingarea'} for e in network.findall('edge')):
        raise ValueError('Этот экспериментальный RL поддерживает только автоперекрёсток без пешеходов. Используйте examples/signal_demo.')
    programs = network.findall('tlLogic')
    if len(programs) != 1: raise ValueError('Для RL-примера нужна сеть ровно с одним светофором')
    phases = programs[0].findall('phase')
    greens = []
    for i, phase in enumerate(phases):
        state = phase.get('state', '')
        if 'G' in state and 'y' not in state.lower():
            following = phases[(i+1) % len(phases)]
            yellow = following.get('state', '')
            if 'y' not in yellow.lower(): raise ValueError('После зелёной фазы должна следовать жёлтая')
            greens.append({'green': state, 'yellow': yellow, 'green_seconds': float(phase.get('duration')),
                           'yellow_seconds': float(following.get('duration'))})
    if not greens: raise ValueError('Не найдено автомобильных зелёных фаз')
    return programs[0].get('id'), greens


def run_episode(config, tls, phases, actions, q, rng, seed, horizon, training=False, epsilon=0, gui=False):
    """Queue cost includes vehicles waiting for insertion; one-second steps."""
    traci.start(['sumo-gui' if gui else 'sumo', '-c', str(Path(config).resolve()), '--seed', str(seed),
                 '--start', '--quit-on-end',
                 '--no-step-log', 'true', '--duration-log.disable', 'true', '--time-to-teleport', '-1'])
    elapsed = 0
    cost = 0
    arrived = 0
    maximum_queue = 0
    used = []
    try:
        lanes = sorted(set(traci.trafficlight.getControlledLanes(tls)))
        def state(index):
            return str(index) + ':' + ','.join(str(min(3, traci.lane.getLastStepHaltingNumber(lane)//3)) for lane in lanes)
        def advance(signal, seconds):
            nonlocal elapsed, cost, arrived, maximum_queue
            traci.trafficlight.setRedYellowGreenState(tls, signal)
            subtotal = 0
            for _ in range(int(seconds)):
                if elapsed >= horizon: break
                traci.simulationStep()
                elapsed += 1
                queue = sum(traci.lane.getLastStepHaltingNumber(lane) for lane in lanes)
                pending = len(traci.simulation.getPendingVehicles())
                subtotal += queue + pending
                arrived += traci.simulation.getArrivedNumber()
                maximum_queue = max(maximum_queue, queue)
            cost += subtotal
            return subtotal
        phase_index = 0
        while elapsed < horizon:
            phase = phases[phase_index]
            key = state(phase_index)
            if actions:
                scores = q.setdefault(key, [0.0] * len(actions))
                action_index = rng.randrange(len(actions)) if training and rng.random() < epsilon else max(range(len(actions)), key=lambda i: scores[i])
                green, yellow, red = actions[action_index]
            else:
                action_index = 0
                green, yellow, red = phase['baseline']
            start = elapsed
            penalty = advance(phase['green'], green)
            penalty += advance(phase['yellow'], yellow)
            penalty += advance('r' * len(phase['green']), red)
            used.append({'phase': phase_index, 'green': green, 'yellow': yellow, 'all_red': red})
            next_index = (phase_index + 1) % len(phases)
            if training:
                future = 0 if elapsed >= horizon else max(q.setdefault(state(next_index), [0.0] * len(actions)))
                old = q[key][action_index]
                # Semi-Markov discount: actions last different numbers of seconds.
                q[key][action_index] = old + .2 * (-penalty + .99**(elapsed-start)*future - old)
            phase_index = next_index
    finally:
        traci.close()
    return {'queue_and_insertion_wait_vehicle_seconds': cost, 'arrived': arrived,
            'maximum_queue_vehicles': maximum_queue, 'decisions': used}


def save_comparison(result, output):
    """Readable results with explicit metric definition and example limitations."""
    rows = []
    for key, label in [('queue_and_insertion_wait_vehicle_seconds','Очередь + ожидание въезда, авто·с'),
                       ('arrived','Прибывшие автомобили'),('maximum_queue_vehicles','Максимальная очередь, авто')]:
        rows.append(f'<tr><td>{label}</td><td>{result["baseline"][key]}</td><td>{result["rl"][key]}</td></tr>')
    decisions = ''.join(f'<tr><td>{d["phase"]}</td><td>{d["green"]}</td><td>{d["yellow"]}</td><td>{d["all_red"]}</td></tr>' for d in result['rl']['decisions'])
    text = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>TrafficAI: RL-пример</title>
    <style>body{{font:17px system-ui;max-width:1000px;margin:40px auto;padding:20px}}table{{border-collapse:collapse;width:100%;margin:20px 0}}td,th{{border:1px solid #ccd5df;padding:10px;text-align:left}}</style>
    <h1>Учебный пример обучения светофора</h1><p>Q-learning. Эпизодов: {len(result['history'])}. Seed обучения: {result['seed']}; контрольного прогона: {result['evaluation_seed']}.</p>
    <p>Автомобильный перекрёсток, без пешеходов. Условия контрольного спроса и случайное зерно одинаковы. Результат одного прогона не гарантирует улучшения на других сетях.</p>
    <p>Диапазоны (секунды): {html.escape(str(result['bounds']))}. Red означает «всем красный», а не красный отдельного направления.</p>
    <h2>Контрольное сравнение</h2><table><tr><th>Показатель</th><th>Фиксированный план</th><th>RL</th></tr>{''.join(rows)}</table>
    <p>Стоимость — сумма числа остановившихся автомобилей на подходах и ожидающих въезда за каждую секунду. Это не среднее время поездки. Исходный план для сравнения приведён к выбранным диапазонам; используется минимальный жёлтый и «всем красный».</p>
    <h2>Решения RL по длительности</h2><table><tr><th>Фаза</th><th>Зелёный, с</th><th>Жёлтый, с</th><th>Всем красный, с</th></tr>{decisions}</table>
    <p>Результаты и политика: results.json, policy.json. Это эксперимент в симуляторе, без проверки пешеходного клиренса и применимости к реальному объекту.</p></html>'''
    (Path(output)/'report.html').write_text(text, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['traci', 'libsumo'], default='traci')
    parser.add_argument('--play-policy', type=Path, help='Replay a previously trained policy without retraining')
    parser.add_argument('--gui', action='store_true', help='Display policy replay in sumo-gui (TraCI only)')
    parser.add_argument('--config', type=Path, default=Path('examples/signal_demo/demo.sumocfg'))
    for color, lower, upper in [('green',15,45), ('yellow',3,5), ('red',2,4)]:
        parser.add_argument('--'+color+'-min', type=int, default=lower)
        parser.add_argument('--'+color+'-max', type=int, default=upper)
    parser.add_argument('--green-step', type=int, default=10)
    parser.add_argument('--episodes', type=int, default=10)
    parser.add_argument('--duration', type=int, default=360)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--output', type=Path, default=Path('runs/rl'))
    args = parser.parse_args()
    global traci
    if args.backend == 'libsumo':
        try:
            import libsumo as traci
        except ImportError as exc:
            parser.error('libsumo недоступен: ' + str(exc))
    if args.episodes < 1 or args.duration < 1: parser.error('Число эпизодов и длительность должны быть положительными')
    try:
        green = duration_choices(args.green_min, args.green_max, args.green_step)
        yellow = duration_choices(args.yellow_min, args.yellow_max, 1)
        red = duration_choices(args.red_min, args.red_max, 1)
        tls, phases = load_program(args.config)
        if args.yellow_min < max(p['yellow_seconds'] for p in phases):
            raise ValueError('Минимальный жёлтый не должен быть короче исходной жёлтой фазы')
    except (ValueError, OSError) as exc: parser.error(str(exc))
    actions = [(g,y,r) for g in green for y in yellow for r in red]
    if len(actions)>500: parser.error('Более 500 действий: уменьшите диапазоны или увеличьте шаг')
    for phase in phases:
        phase['baseline'] = [max(args.green_min,min(args.green_max,phase['green_seconds'])), args.yellow_min,args.red_min]
    q = {}
    rng = random.Random(args.seed)
    history = []
    if args.play_policy:
        if args.backend != 'traci' and args.gui: parser.error('--gui требует --backend traci')
        saved = json.loads(args.play_policy.read_text(encoding='utf-8'))
        if saved.get('actions') != [list(a) for a in actions]: parser.error('Диапазоны действий отличаются от сохранённой политики')
        q = saved['q']
        if any(len(scores)!=len(actions) or not all(math.isfinite(v) for v in scores) for scores in q.values()):
            parser.error('Некорректная таблица Q')
        metrics = run_episode(args.config,tls,phases,actions,q,rng,args.seed+100000,args.duration,gui=args.gui)
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/'replay.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
        print('Воспроизведение завершено:', args.output.resolve())
        return
    for episode in range(args.episodes):
        metrics = run_episode(args.config,tls,phases,actions,q,rng,args.seed+episode,args.duration,True,max(.1,1-episode/args.episodes))
        history.append({k:v for k,v in metrics.items() if k!='decisions'})
        print(f'Эпизод {episode+1}/{args.episodes}: {history[-1]}', flush=True)
    evaluation_seed = args.seed + 100000
    baseline = run_episode(args.config,tls,phases,None,{},rng,evaluation_seed,args.duration)
    learned = run_episode(args.config,tls,phases,actions,q,rng,evaluation_seed,args.duration)
    args.output.mkdir(parents=True,exist_ok=True)
    result = {'method':'tabular Q-learning (experimental)', 'backend':args.backend,'seed':args.seed,'evaluation_seed':evaluation_seed,
              'bounds':{c:[getattr(args,c+'_min'),getattr(args,c+'_max')] for c in ['green','yellow','red']},
              'red_means':'all-red clearance, not the red time of an individual direction',
              'baseline_description':'Original greens clipped to chosen bounds; chosen minimum yellow/all-red. Same demand and SUMO seed.',
              'history':history,'baseline':baseline,'rl':learned,
              'limitations':'One vehicle-only TLS; educational Q-learning, no guaranteed improvement; no field deployment or pedestrian clearance validation.'}
    (args.output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.output/'policy.json').write_text(json.dumps({'actions':actions,'q':q},indent=2),encoding='utf-8')
    save_comparison(result, args.output)
    print('Сравнение и политика:', args.output.resolve(), flush=True)


if __name__ == '__main__': main()
