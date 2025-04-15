import json
import torch
from transformers import AutoTokenizer
from adapters import AutoAdapterModel
from tqdm import tqdm

# Set up device.
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Load base model and tokenizer.
tokenizer = AutoTokenizer.from_pretrained("allenai/specter2_base")
model = AutoAdapterModel.from_pretrained("allenai/specter2_base")
model.to(device)

model.load_adapter("../data/weights/finetuned_adhoc_query_adapter_title", load_as="title")
model.load_adapter("../data/weights/finetuned_adhoc_query_adapter_details", load_as="details")
model.load_adapter("../data/weights/finetuned_adhoc_query_adapter_feature_summary", load_as="feature_summary")
model.to(device)
model.eval()

# Load the training data.
with open("../data/data/3_final_data/final_valid.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# Prebuild a mapping from id to data record to avoid repeated linear searches.
data_dict = {item["id"]: item for item in data}

# Dictionaries to cache embeddings for queries and documents.
# For each id, we will store three separate embeddings.
query_emb_title = {}
query_emb_details = {}
query_emb_feature = {}

doc_emb_title = {}
doc_emb_details = {}
doc_emb_feature = {}

# Precompute and cache embeddings for each data point.
# We compute query embeddings (using each adapter) on the element["query"]
# and document embeddings on the corresponding fields.
with torch.no_grad():
    for element in tqdm(data, desc="Computing embeddings"):
        eid = element["id"]
        query_text = element["query"]
        title_text = element["title"]
        details_text = element["details"]
        feature_text = element["feature_summary"]

        # --- Query embeddings ---
        # Title adapter.
        tokens = tokenizer(query_text, return_tensors="pt", padding=True,
                           truncation=True, max_length=128).to(device)
        model.set_active_adapters("title")
        out = model(**tokens)
        query_emb_title[eid] = out.last_hidden_state[:, 0, :].squeeze(0)

        # Details adapter.
        tokens = tokenizer(query_text, return_tensors="pt", padding=True,
                           truncation=True, max_length=128).to(device)
        model.set_active_adapters("details")
        out = model(**tokens)
        query_emb_details[eid] = out.last_hidden_state[:, 0, :].squeeze(0)

        # Feature summary adapter.
        tokens = tokenizer(query_text, return_tensors="pt", padding=True,
                           truncation=True, max_length=128).to(device)
        model.set_active_adapters("feature_summary")
        out = model(**tokens)
        query_emb_feature[eid] = out.last_hidden_state[:, 0, :].squeeze(0)

        # --- Document embeddings ---
        # For title.
        tokens = tokenizer(title_text, return_tensors="pt", padding=True,
                           truncation=True, max_length=128).to(device)
        model.set_active_adapters("title")
        out = model(**tokens)
        doc_emb_title[eid] = out.last_hidden_state[:, 0, :].squeeze(0)

        # For details.
        tokens = tokenizer(details_text, return_tensors="pt", padding=True,
                           truncation=True, max_length=512).to(device)
        model.set_active_adapters("details")
        out = model(**tokens)
        doc_emb_details[eid] = out.last_hidden_state[:, 0, :].squeeze(0)

        # For feature summary.
        tokens = tokenizer(feature_text, return_tensors="pt", padding=True,
                           truncation=True, max_length=512).to(device)
        model.set_active_adapters("feature_summary")
        out = model(**tokens)
        doc_emb_feature[eid] = out.last_hidden_state[:, 0, :].squeeze(0)

# Define a helper function to compute the dot product between two embeddings.
def dot_product(a: torch.Tensor, b: torch.Tensor) -> float:
    return torch.dot(a, b).item()

# Now, for every query, rank every document using the composite similarity score.
# For a given query-document pair, we compute:
#   sim = (w_title * dot(query_emb_title, doc_emb_title)
#         + w_details * dot(query_emb_details, doc_emb_details)
#         + w_feature * dot(query_emb_feature, doc_emb_feature))
# where weights are assigned as follows:
#   Default: w_title=0.1, w_details=0.6, w_feature=0.3
#   If document's feature_summary == "[UNAVAILABLE]": w_title=0.1, w_details=0.9, w_feature=0.0
ranking = {}

with torch.no_grad():
    for qid in tqdm(query_emb_title.keys(), desc="Ranking queries", unit="query"):
        scores = {}
        # Query embeddings for this query.
        q_title = query_emb_title[qid]
        q_details = query_emb_details[qid]
        q_feature = query_emb_feature[qid]

        for did in doc_emb_title.keys():
            # Document embeddings for this document.
            d_title = doc_emb_title[did]
            d_details = doc_emb_details[did]
            d_feature = doc_emb_feature[did]

            # Check the feature summary field from the original data.
            record = data_dict[did]
            if record["feature_summary"] == "[UNAVAILABLE]":
                w_title, w_details, w_feature = 0.1, 0.9, 0.0
            else:
                w_title, w_details, w_feature = 0.1, 0.6, 0.3

            sim_title = dot_product(q_title, d_title)
            sim_details = dot_product(q_details, d_details)
            sim_feature = dot_product(q_feature, d_feature)

            composite_score = w_title * sim_title + w_details * sim_details + w_feature * sim_feature
            scores[did] = composite_score

        # Rank the document ids for this query in descending order of composite score.
        ranked_doc_ids = sorted(scores, key=scores.get, reverse=True)
        ranking[qid] = ranked_doc_ids

# Export the ranking dictionary to a JSON file.
with open("../data/data/2_intermediate_data/partial_rep_rankings_val.json", "w", encoding="utf-8") as f:
    json.dump(ranking, f, indent=4)

print("Ranking complete. Rankings have been saved to 'rankings.json'.")

# Define evaluation metric function
def eval_metric(weights):
    """
    Given weights [w_title, w_details, w_feature], compute the top-10 accuracy over the
    reliable dataset. For each query, we rank every candidate document (using the same dataset)
    by the composite score computed as the weighted sum of dot products between the query and
    document embeddings. A query is counted as a hit if its own document appears in the top 10.
    """
    w_title, w_details, w_feature = weights
    total = len(query_emb_title)
    correct = 0

    # For each query (keyed by record id)
    for rid in query_emb_title.keys():
        scores = {}
        # Retrieve query embeddings for this record.
        q_title = query_emb_title[rid]
        q_details = query_emb_details[rid]
        q_feature = query_emb_feature[rid]

        # Score every candidate document.
        for cid in doc_emb_title.keys():
            # Compute dot products for each branch.
            sim_title = torch.dot(q_title, doc_emb_title[cid]).item()
            sim_details = torch.dot(q_details, doc_emb_details[cid]).item()
            sim_feature = torch.dot(q_feature, doc_emb_feature[cid]).item()
            # Composite similarity using the candidate weights.
            composite = w_title * sim_title + w_details * sim_details + w_feature * sim_feature
            scores[cid] = composite

        # Rank candidates in descending order (highest similarity first).
        ranked = sorted(scores, key=scores.get, reverse=True)
        # Check if the "relevant" document (same id as the query) is in the top 10.
        if rid in ranked[:10]:
            correct += 1
    return correct / total

# Hill Climbing Optimization

current_weights = [0.1, 0.6, 0.3]
current_metric = eval_metric(current_weights)
print("Initial weights:", current_weights, "Metric:", current_metric)

step_size = 0.05
max_iters = 100
iteration = 0
improved = True

while improved and iteration < max_iters:
    improved = False
    iteration += 1
    for i in range(3):
        for delta in [step_size, -step_size]:
            candidate = current_weights.copy()
            candidate[i] += delta
            # Ensure that weight values do not become negative.
            if candidate[i] < 0:
                continue
            candidate_metric = eval_metric(candidate)
            if candidate_metric > current_metric:
                current_metric = candidate_metric
                current_weights = candidate
                improved = True
                print(f"Iteration {iteration}: Improved weights to {current_weights} with metric {current_metric:.4f}")
    if not improved:
        print(f"No improvement in iteration {iteration}; terminating hill climbing.")
        break

print("Best weights found:", current_weights)
print("Top-10 accuracy (proportion of queries with relevant doc in top 10):", current_metric)