use std::collections::{HashMap, HashSet, VecDeque};

/// Deterministic connected-components labeling over the edges in the
/// current top-K set. Two identical graphs always produce identical
/// component IDs, so the frontend's color mapping stays stable.
///
/// Lifted verbatim (in spirit) from the prior `api/graph.rs` implementation —
/// the state-machine version takes borrowed string slices rather than
/// re-slicing `EdgeAggregate` fields, but the algorithm is the same.
pub fn connected_components<'a>(edges: &[(&'a str, &'a str)]) -> HashMap<&'a str, u32> {
    let mut nodes: HashSet<&str> = HashSet::new();
    let mut adjacency: HashMap<&str, Vec<&str>> = HashMap::new();

    for (from, to) in edges {
        nodes.insert(*from);
        nodes.insert(*to);
        adjacency.entry(*from).or_default().push(*to);
        adjacency.entry(*to).or_default().push(*from);
    }

    let mut sorted_nodes: Vec<&str> = nodes.iter().copied().collect();
    sorted_nodes.sort_unstable();

    let mut temp_id_of: HashMap<&str, usize> = HashMap::with_capacity(nodes.len());
    let mut groups: Vec<Vec<&str>> = Vec::new();

    for &start in &sorted_nodes {
        if temp_id_of.contains_key(start) {
            continue;
        }
        let id = groups.len();
        let mut members: Vec<&str> = Vec::new();
        let mut queue: VecDeque<&str> = VecDeque::from([start]);
        temp_id_of.insert(start, id);
        members.push(start);
        while let Some(node) = queue.pop_front() {
            if let Some(neighbors) = adjacency.get(node) {
                let mut sorted_neighbors: Vec<&str> = neighbors.iter().copied().collect();
                sorted_neighbors.sort_unstable();
                for n in sorted_neighbors {
                    if !temp_id_of.contains_key(n) {
                        temp_id_of.insert(n, id);
                        members.push(n);
                        queue.push_back(n);
                    }
                }
            }
        }
        members.sort_unstable();
        groups.push(members);
    }

    let mut order: Vec<usize> = (0..groups.len()).collect();
    order.sort_unstable_by(|&a, &b| groups[a][0].cmp(groups[b][0]));

    let mut final_id_of_temp: HashMap<usize, u32> = HashMap::with_capacity(groups.len());
    for (rank, temp_id) in order.iter().enumerate() {
        final_id_of_temp.insert(*temp_id, rank as u32);
    }

    temp_id_of
        .into_iter()
        .map(|(wallet, temp_id)| (wallet, final_id_of_temp[&temp_id]))
        .collect()
}
