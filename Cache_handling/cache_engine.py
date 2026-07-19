import json
import numpy as np
import redis.asyncio as aioredis

#  handles all the caching mechanism . To check the cache I am using LSH (which is locality sensitive hashing) we make NUM_PLANES number of random vectors (8 in this case) and we take the dot product with each one of them if the dot product is +ve we set as 1 else we set as 0 . when we do this for all the 8 vectors we get a 8 bit binary number. this effectively partitions the entire space into 2^8 different sections , when we get a vector we just need to find out , which section that vector is (8 dot products) and then we just do a cosine sim check inside all the vectors INSIDE the bucket (section) drastically reducing the number of cosine sim checks needed . 


DIMENSIONS = 384  # output dim for sentence-transformers/all-MiniLM-L6-v2 
NUM_PLANES = 8    

np.random.seed(42)
HYPERPLANES = np.random.randn(NUM_PLANES, DIMENSIONS)
HYPERPLANES /= np.linalg.norm(HYPERPLANES, axis=1, keepdims=True)

def get_lsh_bucket(vector: np.ndarray) -> str:
    """Calculates a projection bitstring to categorize the vector footprint."""
    projections = np.dot(HYPERPLANES, vector)
    bits = (projections > 0).astype(int)
    return "".join(map(str, bits))

def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    dot_product = np.dot(vec_a, vec_b)
    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)
    return float(dot_product / (norm_a * norm_b)) if norm_a and norm_b else 0.0

async def check_cache(redis_client: aioredis.Redis, session_key: str, query_vector: list[float], threshold: float = 0.92) -> str | None:
    """Queries the isolated LSH bucket partition space inside local Redis."""
    query_np = np.array(query_vector, dtype=np.float32)
    bucket = get_lsh_bucket(query_np)
    bucket_redis_path = f"session:{session_key}:bucket:{bucket}"

    member_keys = await redis_client.smembers(bucket_redis_path) # get the members of the bucket
    if not member_keys:
        return None

    async with redis_client.pipeline(transaction=False) as pipe: #instead of making a network request to redis for each member of the bucket , make the entire request at once 
        for key in member_keys:
            await pipe.hmget(key, ["response", "vector"]) # extract the response and the embedding vector for each member
        records = await pipe.execute() # get the data from redis
 
    best_score = -1.0
    best_response = None

    for record in records:
        if not record or not record[1]:
            continue
        cached_res, cached_vec_str = record[0], record[1]
        cached_vec = np.array(json.loads(cached_vec_str), dtype=np.float32)

        similarity = cosine_similarity(query_np, cached_vec)
        if similarity > best_score:
            best_score = similarity
            best_response = cached_res

    if best_score >= threshold:
        return best_response
    return None

async def write_cache(redis_client: aioredis.Redis, session_key: str, query_text: str, query_vector: list[float], response_text: str):
    """Commits new turn computational outputs permanently to the local dynamic index."""
    query_np = np.array(query_vector, dtype=np.float32)
    bucket = get_lsh_bucket(query_np)
    bucket_redis_path = f"session:{session_key}:bucket:{bucket}"

    turn_id = await redis_client.incr("global:turn:counter")
    hash_key = f"turn:{turn_id}"

    payload = {
        "query": query_text,
        "response": response_text,
        "vector": json.dumps(query_vector)
    }

    async with redis_client.pipeline(transaction=True) as pipe:
        await pipe.hset(hash_key, mapping=payload) # make a new hash map with the haskkey (turn id) and the payload (query , response and the embedding vector)
        await pipe.sadd(bucket_redis_path, hash_key) # making a redis set and storing the hash_key to it (storing the hash_key into its apt bucket)
        await pipe.execute()