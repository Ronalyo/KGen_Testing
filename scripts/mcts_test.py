import re
import random
from time import time
from typing import Callable
import heapq as hq

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from transformers import AutoTokenizer, logging, GenerationConfig
from transformers.generation import (
    LogitsProcessorList,
    MinLengthLogitsProcessor,
    MinNewTokensLengthLogitsProcessor,
    TemperatureLogitsWarper,
    TopKLogitsWarper,
    TopPLogitsWarper,
    LogitsProcessor,
    GenerationMode,
    StoppingCriteria,
    StoppingCriteriaList,
)

import kgen.models as models
import kgen.executor.tipo as tipo
from kgen.formatter import seperate_tags, apply_format
from kgen.generate import generate


class LogitsRecorder(LogitsProcessor):
    def __init__(self):
        self.scores = []

    def clean(self):
        self.scores = []

    def __call__(
        self, input_ids: torch.LongTensor, scores: torch.FloatTensor
    ) -> torch.FloatTensor:
        self.scores.append(scores.clone())
        return scores


class NodeSplitter(StoppingCriteria):
    def __init__(self, splitters: list[str, Callable], input_length=0):
        self.splitters = splitters
        self.current = 0
        self.input_length = input_length

    def clean(self, input_length=None):
        self.current = 0
        if input_length is not None:
            self.input_length = input_length

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> bool:
        current = models.tokenizer.decode(input_ids[0])
        for splitter in self.splitters:
            if splitter(current, self.input_length):
                return True
        return False


meta, operations, general, prompt = tipo.parse_tipo_request(
    seperate_tags("masterpiece, 1girl, dragon girl, safe, absurdres".split(",")),
    "A dragon girl",
)
mode, length, expand = operations[0]
prompt = tipo.apply_tipo_prompt(meta, general, prompt, mode, length, expand)

models.load_model(
    "KBlueLeaf/TIPO-500M",
    device="cuda",
)
print(models.text_model.main_input_name)

splitters = [
    lambda x, i: x[i:].split("tags")[-1].split("long:")[0].split("short:")[0].count(",")
### MOD HERE ###
    > 4
### END MOD HERE ###
]
generation_config = GenerationConfig(
    min_new_tokens=4,
    return_dict_in_generate=True,
    output_scores=True,
    do_sample=True,
)

processors = LogitsProcessorList()
recorder = LogitsRecorder()
processors.append(recorder)

stop_criteria = StoppingCriteriaList()
splitter = NodeSplitter(splitters, input_length=len(prompt))
stop_criteria.append(splitter)


def get_next(prompt, input_ids=None, key_values=None):
    recorder.clean()
    splitter.clean(len(prompt))

    if input_ids is None:
        inputs = models.tokenizer(prompt, return_tensors="pt")
        input_ids = inputs["input_ids"].to(next(models.text_model.parameters()).device)
    input_length = input_ids.shape[-1]
    extra_kwargs = {}
    if key_values is not None:
        extra_kwargs["past_key_values"] = key_values
    with torch.no_grad():
        generation_output = models.text_model.generate(
            input_ids=input_ids,
            generation_config=generation_config,
            max_new_tokens=1024,
            logits_processor=processors,
            stopping_criteria=stop_criteria,
            **extra_kwargs,
        )
    output_sequence = generation_output.sequences

### MOD HERE ###
    scores = recorder.scores
    log_total_score = 0
    depth_weight = 0.8 # for prioritizing distant or near tokens
    
    for i, (score, choosed) in enumerate(
        zip(scores[:-1], output_sequence[0][input_length:])
    ):
        if choosed == output_sequence[0][-1]:
            continue
        score = torch.softmax(score, dim=-1)[0]
        token_log_prob = torch.log(score[choosed]).item()
        weight = depth_weight ** (len(scores) - i - 1) # prioritize distant
        # weight = depth_weight ** i # prioritize near
        log_total_score += token_log_prob * weight
        # log_total_score += token_log_prob

    avg_log_score = log_total_score / len(scores) if scores else 0
    avg_score = math.exp(min(avg_log_score, 0))
    # print(avg_score)
### END MOD HERE ###
    
    return (
        output_sequence,
        generation_output.past_key_values,
        models.tokenizer.decode(output_sequence[0]),
        avg_score,
    )

### MOD HERE ###
import math

total_forwards = 0 #DEBUG

class MCTSNode:
    def __init__(self, prompt, input_ids=None, key_values=None, parent=None, depth=0):
        self.prompt = prompt
        self.input_ids = input_ids
        self.key_values = key_values
        self.parent = parent
        self.children = []
        self.depth = depth # for controlling tree width
        
        self.active = False # for caching
        self.score = 0
        self.visits = 0
        self.is_terminal = False
        self.terminal_rank = 0 # for next best
        
    def uct1(self, exploration_weight=0.5): # but father i wish to explor- NO!
        if self.visits == 0:
            return float("inf")
        return self.score + exploration_weight * math.sqrt( math.log(self.parent.visits) / self.visits )
        
def get_variants(prompt, target_variants):
    def best_child(root) -> MCTSNode:
        """
        greedy search for leaf node with max uct1
        stop until no children or active children
        """
        node = root
        while node.children and any(child.active for child in node.children):
            active_children = [c for c in node.children if c.active]
            active_children = [c for c in active_children if not (c.is_terminal and c.terminal_rank > 0)]
        
            if not active_children:
                if node.parent:
                    node.parent.children.remove(node)
                print(f'dead end, create new children') #DEBUG
                return best_child(root)
            
            node = max(active_children, key=lambda c: c.uct1())
        
        return node
            
    def write_results(node, src):
        
        print(f'{src}: terminal reached at {node.depth}')
        if node.score / node.depth < 0.11:
            print(f'but skipped due to low score: {node.score / node.depth}')
        else:
            results.append((node.score, node.depth, node.prompt))
            node.terminal_rank = len(results)
        node.score -= 0.1 * node.score
        backpropagate(node, node.score)
        # if node.parent:
            # if node in node.parent.children:
                # node.parent.children.remove(node)
        # backpropagate(node, node.score * (1 - 0.1 * node.visits))
        return
        
            
    # NOTE: limit max_explore_depth to utilize mcts property
    def rollout(node, max_explore_depth=2) -> float:
        """
        simulate until max_explorate_depth or reaching terminal
        then return deepest node score
        nodes are created along simulation path but remain inactive until expansion
        max_explore_depth relative to node.depth
        """
        current_node = node
        current_depth = 0
        
        while current_depth < max_explore_depth:
        # while True:
            #DEBUG
            global total_forwards
            total_forwards += 1
            
            output_sequence, past_key_values, decoded, score = get_next(
                current_node.prompt,
                current_node.input_ids,
                current_node.key_values,
            )
            
            is_terminal = output_sequence[0][-1] == models.tokenizer.eos_token_id
            
            child = MCTSNode(
                prompt=decoded,
                input_ids=output_sequence,
                key_values=past_key_values,
                parent=current_node,
                depth=current_node.depth + 1,
            )
            child.score = score
            child.is_terminal = is_terminal
            current_node.children.append(child)
            
            if is_terminal:
                write_results(child, 'rollout')
                break
            else:
                current_node = child
                current_depth += 1
                
        return current_node.score
            
            
    def expand(node) -> None:
        """
        convert inactive children to active for current node if any exist
        create childrens for visited nodes
        until node has max(2, 4-node.depth) children
        """
    
        num_children = max(2, 4 - node.depth)
        while len(node.children) < num_children:
            #DEBUG
            global total_forwards
            total_forwards += 1
            
            output_sequence, past_key_values, decoded, score = get_next(
                node.prompt,
                node.input_ids,
                node.key_values,
            )
            
            child = MCTSNode(
                prompt=decoded,
                input_ids=output_sequence,
                key_values=past_key_values,
                parent=node,
                depth=node.depth + 1,
            )
            child.score = score
            child.is_terminal = output_sequence[0][-1] == models.tokenizer.eos_token_id
            node.children.append(child)
            if child.is_terminal:
                write_results(child, 'expand')
            
        for c in node.children:
            c.active = True
    
    def backpropagate(node, score) -> None:
        """
        update from rollout node upward to root
        """
        while node is not None:
            node.visits += 1
            node.score += score
            node = node.parent
    
    results = []
    
    root = MCTSNode(prompt)
    root.active = True
    
    #DEBUG
    iter = 0
    while len(results) < target_variants:
        # select max uct1
        node = best_child(root)
        
        # print(f'selected node: {node.depth}, {node.input_ids[0][-2] if node.input_ids is not None else None}') #DEBUG
        
        if not node.is_terminal:
            # if node unvisited: rollout and backprop
            # otherwise expand only
            if node.visits == 0:
                if iter % 10 == 0:
                    print(f"iter: {iter} - results: {len(results)}") #DEBUG
                iter += 1
                score = rollout(node)
                backpropagate(node, score)
            else:
                expand(node)
        else:
            write_results(node, 'search')
    
    print(f'fowards: {total_forwards}')
    return results

### END MOD HERE ###

results = (
    get_variants(prompt, target_variants=7)
    # + get_variants(prompt, target_variants=3)
    # + get_variants(prompt, target_variants=3)
)

for score, level, result in sorted(results, key=lambda x: x[0] / x[1], reverse=False):
    print(f"{score/level}")
    print("-" * 20)
    print(f"{result}")
    print("=" * 50)
