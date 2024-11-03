from typing import Optional

import numpy as np

import kgen.models as models
import kgen.executor.tipo as tipo
from kgen.formatter import seperate_tags, apply_format
from kgen.generate import generate
from kgen.sampling import SampleNode, LogitsRecorder, NodeSplitter, get_next, draw_tree


class MCTSNode(SampleNode):
    parent: "MCTSNode"
    childs: list["MCTSNode"]

    def __init__(
        self, prompt=None, inputs=None, past_key_values=None, score=0, parent=None
    ):
        super().__init__(prompt, inputs, past_key_values, score, parent)
        # MCTS specific properties
        self.visit_count = 0
        self.total_value = 0
        self.self_visit_count = 0
        self.self_total_value = 0
        self.simulated_result: Optional[str] = None

    def get_uct_score(self, exploration_weight=1.0, self_uct=False):
        if self.visit_count == 0:
            return float("inf")

        if self_uct:
            # calc UCT score on "virtual child" (self)
            # when this been selected, it means we add new child into this node
            exploitation = self.self_total_value / self.self_visit_count
            exploration = exploration_weight * np.sqrt(
                np.log(self.visit_count) / self.self_visit_count
            )
        else:
            exploitation = self.total_value / self.visit_count
            exploration = exploration_weight * np.sqrt(
                np.log(self.parent.visit_count) / self.visit_count
            )
        return exploitation + exploration

    def select_child(self, exploration_weight=1, random_walk=False) -> "MCTSNode":
        if all(c.is_leaf for c in self.childs):
            return self
        ##Calculate scores for visited children
        scores = np.array(
            [child.get_uct_score(exploration_weight) for child in self.childs]
            + [self.get_uct_score(0.5, self_uct=True)]
        )
        if random_walk:
            ##Select child based on softmax probabilities
            ##Random Walk MCTS to immitate the behavior of sampling in LLM
            scores = np.exp(scores - np.max(scores))  # Softmax normalization
            scores = scores / scores.sum()
            chosen_idx = np.random.choice(len(self.childs) + 1, p=scores)
        else:
            ##Select child based on score (original MCTS)
            chosen_idx = np.argmax(scores)
        return (self.childs + [self])[chosen_idx]

    def expand(self, record_simulated_path=False) -> tuple["MCTSNode", int]:
        print("Expand")
        # Generate new child
        recorder = LogitsRecorder()
        splitters = [lambda x, i: (x[i:].split("tags")[-1].count(",") > 4)]
        splitter = NodeSplitter(splitters, input_length=len(self.prompt))

        # Get the immediate next node
        # this score will be taken as "simulation result"
        total_gen = 1
        out_seq, past_key_values, decode, score = get_next(
            self.prompt,
            input_ids=self.inputs,
            key_values=self.past_key_values,
            recorder=recorder,
            splitter=splitter,
        )

        ## Expansion + apply rollout(simulation) result
        new_child = MCTSNode(
            prompt=decode,
            inputs=out_seq,
            past_key_values=past_key_values,
            score=score,
            parent=self,
        )
        ## backpropagate score
        new_child.backpropagate(score)
        self.childs.append(new_child)
        ## self visit mechanism in our proposed method
        self.self_visit_count += 1
        self.self_total_value += new_child.score

        # Simulate a complete path from this child
        gen = 0
        cur = new_child
        while not cur.is_leaf:
            total_gen += 1
            gen += 1
            inputs, past_key_values, prompt, score = get_next(
                cur.prompt,
                input_ids=cur.inputs,
                key_values=cur.past_key_values,
                recorder=recorder,
                splitter=splitter,
            )
            if record_simulated_path:
                ## this implementation make the simulation path into MCTS tree directly
                cur.childs.append(
                    MCTSNode(
                        prompt=prompt,
                        inputs=inputs,
                        past_key_values=past_key_values,
                        score=score,
                        parent=cur,
                    )
                )
                cur.self_total_value += score
                cur.self_visit_count += 1
                cur = cur.childs[-1]
                cur.backpropagate(score)
            else:
                ## This implementation ignore the procedure of simulation,
                ## just take the final result
                cur = MCTSNode(
                    prompt=prompt,
                    inputs=inputs,
                    past_key_values=past_key_values,
                    score=score,
                )
        print("Simulate end")

        new_child.simulated_result = (
            prompt.replace("<s>", "").replace("</s>", "").strip(),
            new_child.depth + gen,
        )
        return new_child, total_gen

    def backpropagate(self, value: float):
        node = self
        while node is not None:
            node.visit_count += 1
            node.total_value += value
            node = node.parent


def mcts_sample(
    prompt: str,
    variations: int = 7,
    exploration=1.0,
    random_walk=False,
    solid_simulate=False,
):
    root = MCTSNode(prompt=prompt)
    results = []
    total_iterations = 0
    total_gen = 0
    exploration_weight = 8.0 if random_walk else 1.0
    exploration_weight = exploration_weight * exploration

    while len(results) < variations:
        # Selection
        node = root
        depth = 0
        while node.childs:
            depth += 1
            next = node.select_child(
                exploration_weight=exploration_weight / depth,
                random_walk=random_walk,
            )
            if next is node or next.is_leaf:
                depth -= 1
                break
            node = next
        print(f"Select depth: {depth}")

        # Expansiona + rollout + backpropogation
        new_node, gen = node.expand(
            record_simulated_path=random_walk and solid_simulate
        )
        total_gen += gen
        # If we got a complete generation, add it to results
        if new_node.is_leaf or new_node.simulated_result is not None:
            print("Add result")
            results.append(new_node.simulated_result)

        total_iterations += 1

    print(f"Total iterations: {total_iterations}")
    print(f"Total generations: {total_gen}")
    return results, root


def _count(node: MCTSNode, depth: int = 0, total_childs=None, total_nodes=None):
    if node.is_leaf:
        return
    if depth not in total_childs:
        total_childs[depth] = 0
        total_nodes[depth] = 0
    total_childs[depth] += len(node.childs)
    total_nodes[depth] += 1
    for child in node.childs:
        _count(child, depth + 1, total_childs, total_nodes)


def count(node: MCTSNode):
    total_childs = {}
    total_nodes = {}
    _count(node, total_childs=total_childs, total_nodes=total_nodes)
    return total_childs, total_nodes


if __name__ == "__main__":
    models.load_model(
        "KBlueLeaf/TIPO-100M",
        device="cuda",
    )

    meta, operations, general, prompt = tipo.parse_tipo_request(
        seperate_tags("1girl, fox girl, fox ears, multiple tails".split(",")),
        "",
    )
    mode, length, expand = operations[0]
    prompt = tipo.apply_tipo_prompt(meta, general, prompt, mode, length, expand)

    # results, root = mcts_random_walk(prompt, variations=9)
    results, root = mcts_sample(
        prompt,
        variations=16,
        exploration=1.0,
        random_walk=True,
        solid_simulate=False,
    )
    dot = draw_tree(root)
    dot.render("tree16", cleanup=True, format="png")
    total_childs, total_nodes = count(root)

    count(root)
    print(f"Total nodes per depth: {total_nodes}")
    print(
        "Average childs per node per depth: "
        f"{[total_childs[i] / total_nodes[i] for i in range(len(total_childs))]}"
    )
    gen_per_prompt = [x[1] for x in results]
    print(sum(gen_per_prompt) / len(gen_per_prompt))
    for result, gen in sorted(results):
        print("=" * 20)
        print(result)
    print("=" * 20)