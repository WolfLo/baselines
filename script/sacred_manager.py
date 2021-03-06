'''
    Manages the sacred runs directory:
    - Stats of the directory: number of runs, how many completed
    - Clean uncompleted runs
    - Remove COUT files.
'''

import json, re, argparse, glob, shutil, os
from collections import defaultdict

def load_runs(base_directory):
    if base_directory[-1] != '/':
        base_directory += '/'
    runs = {}
    runs_filenames = glob.glob(base_directory + '*/config.json')
    run_extractor = re.compile(base_directory + '([0-9]+)/config.json')
    for r in runs_filenames:
        try:
            run_number = int(run_extractor.match(r).group(1))
            runs[run_number] = {}
            runs[run_number]['config'] = json.load(open(base_directory + str(run_number) + '/config.json'))
            runs[run_number]['run'] = json.load(open(base_directory + str(run_number) + '/run.json'))
            runs[run_number]['metrics'] = json.load(open(base_directory + str(run_number) + '/metrics.json'))
        except:
            del runs[run_number]
    return runs

def recursive_json_selector(obj, selector):
    try:
        if selector is not None:
            for qk in selector.split('.'):
                obj = obj[qk]
        return obj
    except:
        return None

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--dir', help='Directory of the sacred runs.', default='sacred_runs')
parser.add_argument('--command', help='Different commands of the manager.', default='stats', choices=['stats', 'clean'])
parser.add_argument('--groupby', help='Count groups by a specified config parameter (stats mode only).', default=None)
parser.add_argument('--uncompleted', default=False, action='store_true')
parser.add_argument('--cout', default=False, action='store_true')
parser.add_argument('--filter', help='Remove the runs filtered as specified [key==value]', default=None)
args = parser.parse_args()

my_runs = load_runs(args.dir)

base_directory = args.dir
if base_directory[-1] != '/':
    base_directory += '/'

if args.command == 'stats':
    print("------- SACRED STATS -------")
    print("Total number of runs:", len(my_runs.keys()))
    # Get completed runs
    print("Total number of completed runs:", len([key for key, value in my_runs.items() if value['run']['status'] == 'COMPLETED']))
    # Groupby
    if args.groupby is not None:
        counter = defaultdict(int)
        for key, value in my_runs.items():
            counter[recursive_json_selector(value, args.groupby)] += 1
        # Print
        print("\nGroupby:", args.groupby)
        for key, value in counter.items():
            print("\t", key, ":", value)

elif args.command == 'clean':
    removed_runs = 0
    # Parse the filter argument
    if args.filter is not None:
        filter_key, filter_value = args.filter.split('==')

    for key, value in my_runs.items():
        if value['run']['status'] != 'COMPLETED' and args.uncompleted:
            # Remove run with key
            shutil.rmtree(base_directory + str(key) + '/')
            print("Removed run:", key)
            removed_runs += 1
        elif args.cout:
            # Try to remove the cout file
            try:
                os.remove(base_directory + str(key) + '/cout.txt')
                print("Removed cout for run:", key)
            except:
                pass
        elif args.filter is not None:
            # Check if the filter applies
            selected_value = recursive_json_selector(value, filter_key)
            if selected_value is not None and str(selected_value) == filter_value:
                # Remove run with key
                shutil.rmtree(base_directory + str(key) + '/')
                print("Removed run:", key)
                removed_runs += 1

    print("Completed. Removed a total of", removed_runs, "runs.")
else:
    raise Exception('Unrecognized command.')
