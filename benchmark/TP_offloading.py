import os
import sys
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(root_dir)

import torch.multiprocessing as mp
import torch.distributed as dist
import torch
import argparse
from termcolor import colored
from utils.batch_decoding import Baseline_Dist, Retrieval_Spec_Dist
from models.TP_llama import distributed_init, DistributedLlama
from models.modeling_llama import LlamaForCausalLM
from transformers import AutoTokenizer
import numpy as np
import time
from torch.nn.functional import softmax
from utils.tree_infer import GraphInferenceEngine, get_sampling_logits, cuda_graph_for_residual, cuda_graph_for_sampling_without_replacement, get_residual
from utils.SpecTree_TP import SpecTree

local_rank, world_size = distributed_init()
device = torch.device("cuda", local_rank)
model_name_or_path = "NousResearch/Yarn-Llama-2-7b-128k"

def create_sampling_callable(num_samples, temperature=0.6):
    def sampling_without_replacement(sampling_logits: torch.Tensor, static_rand):
        if torch.distributed.get_rank() == 0:
            sampling_q = softmax(sampling_logits / temperature, dim=-1)
            position = (static_rand.log()/sampling_q).topk(k=num_samples).indices.flatten()
        else:
            position = torch.full((num_samples * sampling_logits.shape[0],), -1, dtype=torch.long, device=sampling_logits.device)
        torch.distributed.broadcast(position, src=0)
        return position
    
    return sampling_without_replacement

def parse_arguments():
    parser = argparse.ArgumentParser(description='args for main.py')
    parser.add_argument('--prefill', type=int, default=130048, help='prefill length')
    parser.add_argument('--gen_len', type=int, default=64, help='generation length')
    parser.add_argument('--temp', type=float, default=0.6, help='temperature')
    parser.add_argument('--budget', type=int,  default=4096)
    parser.add_argument('--tree_size', type=int, default=512)
    parser.add_argument('--ssl', type=int, default=0)
    parser.add_argument('--max_width', type=int, default=64)
    args = parser.parse_args()
    
    return args

args = parse_arguments()

prefill = args.prefill
gen_len = args.gen_len
temperature = args.temp
top_p = 0.9
retrieval_budget = args.budget
tree_size = args.tree_size
ssl = args.ssl
max_width = args.max_width

if local_rank == 0:
    print(f"Config: {prefill=}, {retrieval_budget=}, {tree_size=}, {ssl=}, {max_width=}")


tokenizer = AutoTokenizer.from_pretrained(model_name_or_path, use_fast=True, legacy=False)
llm = DistributedLlama(model_name_or_path=model_name_or_path, local_rank=local_rank, world_size=world_size, prefill=prefill, gen_len=gen_len, temperature=temperature, top_p=top_p, flash_attn=True, retrieval_budget=retrieval_budget, kv_offload=True, on_chip_layers=8, tree_size=args.tree_size, ssl=ssl)
for rank in range(world_size):
    if local_rank == rank:
        print(f"Rank {rank+1}/{world_size} (Device {device}) is initializing parameters")
        hf_model = LlamaForCausalLM.from_pretrained(model_name_or_path, torch_dtype=torch.float16, device_map='cpu')
        llm.init_parameters(hf_model=hf_model)
        del hf_model
    dist.barrier()


T = 100
with torch.inference_mode():
    llm.kv_cache.seq_len = prefill
    
    ######### Draft #########
    next_token = torch.randint(3, 30000, (1, max_width), device=device)
    position_ids = torch.tensor([[retrieval_budget for _ in range(max_width)]], dtype=torch.long, device=device)
    storage_ids = torch.tensor([x for x in range(retrieval_budget, retrieval_budget+max_width)], dtype=torch.long, device=device)
    attention_mask = torch.cat([torch.zeros(max_width, retrieval_budget, device=device), torch.zeros(max_width, tree_size, device=device)], dim=-1)[None, None, :, :]
    llm.retrieval_cache.normal_()
    
    # warm up
    for k in range(10):
        llm.retrieval_tree_inference(input_ids = next_token, position_ids = position_ids, attention_mask=attention_mask, storage_ids=storage_ids)
        if k % 2 == 0:
            llm.kv_cache.ssl_cur = 0
    
    torch.cuda.synchronize()
    start = time.time()
    for k in range(T):
        llm.retrieval_tree_inference(input_ids = next_token, position_ids = position_ids, attention_mask=attention_mask, storage_ids=storage_ids)
        if k % 2 == 0:
            llm.kv_cache.ssl_cur = 0
    torch.cuda.synchronize()
    end = time.time()
    draft_time = (end-start) / T
    if local_rank == 0:
        print(f"Draft Time (Tree size: {tree_size}): {draft_time}")



    # llm.kv_cache.normal_(prefill)
    llm.kv_cache.seq_len = prefill
    depth = torch.arange(0, args.tree_size, device=device, dtype=torch.int32).unsqueeze(0)
    tree_mask = torch.zeros(args.tree_size, args.tree_size, device=device)
    sentence = torch.randint(3, 30000, (1, tree_size), device=device)
    position_ids = (depth + llm.kv_cache.seq_len)
    attn_mask = torch.cat([torch.zeros(tree_size, llm.kv_cache.seq_len, device=llm.device), tree_mask], dim=-1)[None, None, :, :]

    # print(sentence, position_ids, attn_mask.shape)
    # warm up
    for _ in range(10):
        llm.inference(input_ids = sentence, position_ids=position_ids, attention_mask=attn_mask)
        llm.kv_cache.seq_len = prefill
    
    torch.cuda.synchronize()
    start = time.time()
    for _ in range(T):
        llm.inference(input_ids = sentence, position_ids=position_ids, attention_mask=attn_mask)
        llm.kv_cache.seq_len = prefill
    torch.cuda.synchronize()
    end = time.time()
    verify_time = (end-start) / T
    if local_rank == 0:
        print(f"Verification Time (Tree size: {tree_size}): {verify_time}")
        with open("benchmark/TP_offloading.txt", "a") as f:
            f.write(f"{retrieval_budget},{tree_size},{max_width},{ssl},{draft_time},{verify_time}\n")