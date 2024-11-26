
# MAGIC Instructions


## Variables
we only mention variables you should specifically consider in this method, others like variation, tags per node are not listed

### Hyperparameters for Function Call:
- **Exploration Weight**: exploration factor used in UCT, default is 1.4
    

### Variables in Implementation:
ones with no definitive reason
- **Progressive widening hyperparameters** in `MCTSNode.expand`:
    - `k`: how wide the tree should be, default can be beam_width in original beam search (depth-dependent), should be at least 1, which would min_width
    - `alpha`: growth rate moderator, default is 0.5, should be in [0, 1], 0 means fixed width (k)
- **Scoring method parameter** for `get_next()`:
    - `scoring`: how to score the result, default is `default`, 've also added `log`

## Things you should know
### Nodes without children?
**Why?** each expansion phase creates a dynamically allocated number of children, and greedy can select from them the best according to uct1  
**The idea** is that if exploration and PW is tuned just right, uct1 would go through every promising node this layer until next layer's nodes would yield better results
    
### Getting stuck?
**Why?** in the case of low exploration, very high variant count etc.,  greedy select might get stuck in the same path  
**Currently this is not handled**, we're trying to make sure the tree is wide enough so it never happens, as it should only happen when diversity is very low.

### Why cache?
**Why?** originally it was designed so expand can save without *at least* one less get_next() call    
**But** the same node once simulated from, will never be used as simulation source again, so effectively, it's *exactly* one node saved, per expansion, maybe it's not worth it