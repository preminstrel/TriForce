import os
import sys
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(root_dir)

print(f"added {root_dir} to sys.path")

import torch
from transformers import AutoTokenizer
from termcolor import colored

from models.modeling_llama_torch import LlamaForCausalLM
from models.cache_utils import SimpleCache, EvictStreamLLMCache, StreamLLMCache
from utils.decoding import Evict_Spec_Evict
from utils.misc import print_config

import argparse
def parse_arguments():
    parser = argparse.ArgumentParser(description='args for main.py')

    parser.add_argument('--target', type=str, default='llama-7B-128K', help='target model')
    parser.add_argument('--draft', type=str, default='llama-68m-256', help='draft model')
    parser.add_argument('--verbose', action='store_true', help='verbose')
    parser.add_argument('--greedy', action='store_true', help='greedy')

    parser.add_argument('--prefill', type=int, default=32768, help='prefill length')
    parser.add_argument('--gen_len', type=int, default=256, help='generation length')
    parser.add_argument('--gamma', type=int, default=1, help='gamma')
    parser.add_argument('--log_csv', action='store_true', help='log_csv')
    parser.add_argument('--temp', type=float, default=0.6, help='temperature')

    parser.add_argument('--dataset', type=str, default='benchmark', help='dataset')
    args = parser.parse_args()
    
    return args

args = parse_arguments()

######## model initialization ########

if args.draft == 'llama-68m-256':
    draft = LlamaForCausalLM.from_pretrained("JackFram/llama-68m", torch_dtype=torch.float16, device_map="auto")
    # draft = LlamaForCausalLM.from_pretrained("/home/hanshis/workspace/LongContextInfer/archive/ckpts/512/step_125", torch_dtype=torch.float16, device_map="auto")
elif args.draft == 'llama-160m':
    draft = LlamaForCausalLM.from_pretrained("JackFram/llama-160m", torch_dtype=torch.float16, device_map="auto")
elif args.draft == 'llama-7B':
    # draft = LlamaForCausalLM.from_pretrained("/home/hanshis/workspace/NNSPD/models/7B", torch_dtype=torch.float16, device_map="cuda:1")
    draft = LlamaForCausalLM.from_pretrained("NousResearch/Yarn-Llama-2-7b-128k", torch_dtype=torch.float16, device_map="cuda:1")
else:
    draft = LlamaForCausalLM.from_pretrained(args.draft, torch_dtype=torch.float16, device_map="auto")

if args.target == 'llama-7B-128K':
    target = LlamaForCausalLM.from_pretrained("NousResearch/Yarn-Llama-2-7b-128k", torch_dtype=torch.float16, device_map="auto")
else:
    target = LlamaForCausalLM.from_pretrained(args.target, torch_dtype=torch.float16, device_map="auto")

draft = draft.eval()
target = target.eval()

tokenizer = AutoTokenizer.from_pretrained("JackFram/llama-68m", legacy=False, use_fast=True)

######## sampling parameters ########

if args.greedy:
    top_k = 1
    top_p = 1
    temperature = 1
else:
    top_k = -1
    top_p = 0.9
    temperature = args.temp

from data.dataset import get_dataset
tokenized_prompts = get_dataset(dataset_name=args.dataset, tokenizer=tokenizer, datalen=args.prefill)

prefill = args.prefill
gen_len = args.gen_len
gamma = args.gamma
verbose = args.verbose

if args.log_csv:
    import socket
    host = socket.gethostname()
    if 'lovelace' in host:
        file_path = "/home/hanshis/workspace/LongContextInfer/test/report/L40_evict4evict.csv"
    else:
        file_path = "/data/home/beidic/hanshi/LongContextInfer/test/report/A100_evict4evict.csv"
else:
    file_path = None



# print_config(draft, target, prefill, gen_len, gamma, top_k, top_p, temperature, file_path=file_path, method="Evict StreamLLM", spec_args={'start_size': 16, 'recent_size': 512-16}, dataset=args.dataset)


draft_cache = EvictStreamLLMCache(draft, start_size=16, recent_size=256-16)
target_cache = EvictStreamLLMCache(target, start_size=16, recent_size=4096-16)

all_acceptance_rate = []
print(colored(f"tokenized_prompts length: {len(tokenized_prompts)}", "green"))

for input_ids in tokenized_prompts:
    if prefill < 4096:
        input_ids = input_ids.to(draft.device)[:,2048:prefill+2048]
    else:
        input_ids = input_ids.to(draft.device)[:,:prefill]

    acceptance_rate = Evict_Spec_Evict(tokenizer, target, target_cache, draft, draft_cache, input_ids, gamma=gamma, max_len=gen_len, top_k=top_k, top_p=top_p, temperature=temperature, verbose=verbose, file_path=file_path, dataset=args.dataset)
    all_acceptance_rate.append(acceptance_rate)

print(colored(f"average acceptance rate: {sum(all_acceptance_rate) / len(all_acceptance_rate)}", "red"))