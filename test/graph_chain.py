import os
import sys
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(root_dir)

import torch
from transformers import AutoTokenizer
from termcolor import colored

from models.modeling_llama import LlamaForCausalLM
from models.modeling_llama_68m_v2 import LlamaForCausalLM as LlamaForCausalLM_68M
from models.cache_utils import FlashSimpleCache, GraphFlashStreamEvictionCache_V2, GraphFlashStreamLLMVerificationCache, GraphFlashChunkTopKVerificationCache
from utils.decoding import Graph_Chain_V2
from utils.misc import print_config
from utils.chain_infer import GraphInferenceEngine
import socket
host = socket.gethostname()
import argparse
def parse_arguments():
    parser = argparse.ArgumentParser(description='args for main.py')

    parser.add_argument('--target', type=str, default='llama-7B-128K', help='target model')
    parser.add_argument('--draft', type=str, default='llama-68M', help='draft model')
    parser.add_argument('--verbose', action='store_true', help='verbose')
    parser.add_argument('--greedy', action='store_true', help='greedy')

    parser.add_argument('--prefill', type=int, default=32768, help='prefill length')
    parser.add_argument('--gen_len', type=int, default=256, help='generation length')
    parser.add_argument('--gamma', type=int, default=6, help='gamma')
    parser.add_argument('--log_csv', action='store_true', help='log_csv')

    parser.add_argument('--dataset', type=str, default='benchmark', help='dataset')

    parser.add_argument('--budget', type=float, default='0.1')
    args = parser.parse_args()
    
    return args

args = parse_arguments()

######## model initialization ########
if args.target == 'llama-7B':
    target = LlamaForCausalLM.from_pretrained("/home/hanshis/workspace/NNSPD/models/7B", torch_dtype=torch.float16, device_map="auto")
elif args.target == 'llama-7B-32K':
    target = LlamaForCausalLM.from_pretrained("togethercomputer/LLaMA-2-7B-32K", torch_dtype=torch.float16, device_map="auto")
elif args.target == 'llama-7B-128K':
    target = LlamaForCausalLM.from_pretrained("NousResearch/Yarn-Llama-2-7b-128k", torch_dtype=torch.float16, device_map="auto")
elif args.target == 'llama-7B-1M':
    target = LlamaForCausalLM.from_pretrained("LargeWorldModel/LWM-Text-1M", torch_dtype=torch.float16, device_map="auto")
else:
    raise NotImplementedError

target = target.eval()
tokenizer = AutoTokenizer.from_pretrained("NousResearch/Yarn-Llama-2-7b-128k", use_fast=True, legacy=False)
from data.dataset import get_dataset
tokenized_prompts = get_dataset(dataset_name=args.dataset, tokenizer=tokenizer, datalen=args.prefill)

######## sampling parameters ########

if args.greedy:
    top_k = 1
    top_p = 1
    temperature = 1
else:
    top_k = -1
    top_p = 0.9
    temperature = 0.8

prefill = args.prefill
gen_len = args.gen_len
gamma = args.gamma
verbose = args.verbose

if args.log_csv:
    if 'lovelace' in host:
        file_path = "/home/hanshis/workspace/LongContextInfer/test/report/L40_graph_chain.csv"
    else:
        file_path = "/data/home/beidic/hanshi/LongContextInfer/test/report/A100_graph_chain.csv"
else:
    file_path = None

if 'lovelace' in host:
    align_ckpt = "/home/hanshis/workspace/LongContextInfer/archive/ckpts/512/step_125"
else:
    align_ckpt = "/fsx-storygen/beidic/hanshi/ckpts/Base-128K-256/step_11696"

print_config(target, target, prefill, gen_len, gamma, top_k, top_p, temperature, file_path=file_path, method="Graph Chain Spec", spec_args={'budget': args.budget, 'draft': args.draft}, dataset=args.dataset)

start_size = 16 + prefill // 1024
recent_size = int(args.budget * prefill) - start_size

if recent_size + start_size < 4096:
    start_size = 20
    recent_size = 4096 - start_size

# draft = LlamaForCausalLM_68M.from_pretrained("/home/hanshis/workspace/LongContextInfer/archive/ckpts/512/step_125", torch_dtype=torch.float16, device_map="auto")
if args.draft == 'llama-68M':
    draft = LlamaForCausalLM_68M.from_pretrained("JackFram/llama-68m", torch_dtype=torch.float16, device_map="auto")
elif args.draft == 'llama-68M-align':
    draft = LlamaForCausalLM_68M.from_pretrained(align_ckpt, torch_dtype=torch.float16, device_map="auto")
else:
    raise NotImplementedError

####### cache init #######
cache = FlashSimpleCache(target, prefill+gen_len+16)
graph_cache = GraphFlashStreamLLMVerificationCache(target, max_budget=start_size+recent_size, prefill=prefill, gamma=gamma, start_size=start_size)
draft_cache = GraphFlashStreamEvictionCache_V2(draft, start_size=16, recent_size=250-16, gamma=gamma)

graph_engine = GraphInferenceEngine(target, cache, graph_cache, draft, draft_cache)
graph_engine.initialize_cuda_graph(gamma, probs=True)

cache.print_status()
graph_cache.print_status()
draft_cache.print_status()

all_acceptance_rate = []
print(colored(f"tokenized_prompts length: {len(tokenized_prompts)}", "green"))

for input_ids in tokenized_prompts:
    input_ids = input_ids.to(target.device)[:,:prefill]

    acceptance_rate = Graph_Chain_V2(tokenizer, graph_engine, input_ids, gamma=gamma, max_len=gen_len, top_k=top_k, top_p=top_p, temperature=temperature, verbose=verbose, file_path=file_path, dataset=args.dataset, spec_args={'budget': args.budget})
    all_acceptance_rate.append(acceptance_rate)

print(colored(f"average acceptance rate: {sum(all_acceptance_rate) / len(all_acceptance_rate)}", "red"))