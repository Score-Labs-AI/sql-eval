import os
from time import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

import pandas as pd
import sqlparse
from tqdm import tqdm

from eval.eval import compare_query_results
from utils.creds import db_creds_all
from utils.dialects import convert_postgres_ddl_to_dialect
from utils.gen_prompt import to_prompt_schema
from utils.questions import prepare_questions_df
from utils.reporting import upload_results
from utils.llm import chat_openai

import logging 
from  datetime import datetime


def log_interaction(prompt,response, filtered_response, golden_query, correct, error_msg, correct_sofar):
    log_entry = f"PROMPT: {prompt}\nRESPONSE: {response}\nFILTERED_RESPONSE: {filtered_response}\nGOLDEN_QUERY: {golden_query}\nCORRECT: {correct}\nERROR_MESSAGE: {error_msg}\nCORRECT_SOFAR: {correct_sofar}"
    log_entry += "\n" + '-' * 100
    logging.info(log_entry)  

def generate_prompt(
    prompt_file,
    question,
    db_name,
    db_type,
    instructions="",
    k_shot_prompt="",
    table_metadata_string="",
    public_data=True,
    shuffle=True,
):
    if public_data:
        from defog_data.metadata import dbs
        import defog_data.supplementary as sup
    else:
        from defog_data_private.metadata import dbs
        import defog_data_private.supplementary as sup

    with open(prompt_file, "r") as f:
        prompt = json.load(f)

    if table_metadata_string == "":
        md = dbs[db_name]["table_metadata"]
        pruned_metadata_ddl = to_prompt_schema(md, shuffle)
        pruned_metadata_ddl = convert_postgres_ddl_to_dialect(
            postgres_ddl=pruned_metadata_ddl,
            to_dialect=db_type,
            db_name=db_name,
        )
        column_join = sup.columns_join.get(db_name, {})
        join_list = []
        for values in column_join.values():
            if isinstance(values[0], tuple):
                for col_pair in values:
                    col_1, col_2 = col_pair
                    join_str = f"{col_1} can be joined with {col_2}"
                    if join_str not in join_list:
                        join_list.append(join_str)
            else:
                col_1, col_2 = values[0]
                join_str = f"{col_1} can be joined with {col_2}"
                if join_str not in join_list:
                    join_list.append(join_str)
        if len(join_list) > 0:
            join_str = "\nHere is a list of joinable columns:\n" + "\n".join(join_list)
        else:
            join_str = ""
        pruned_metadata_str = pruned_metadata_ddl + join_str
    else:
        pruned_metadata_str = table_metadata_string

    if prompt[0]["role"] == "system":
        prompt[0]["content"] = prompt[0]["content"].format(
            db_type=db_type,
        )
        prompt[1]["content"] = prompt[1]["content"].format(
            user_question=question,
            instructions=instructions,
            table_metadata_string=pruned_metadata_str,
            k_shot_prompt=k_shot_prompt,
        )
    else:
        prompt[0]["content"] = prompt[0]["content"].format(
            db_type=db_type,
            user_question=question,
            instructions=instructions,
            table_metadata_string=pruned_metadata_str,
            k_shot_prompt=k_shot_prompt,
        )

    return prompt


def process_row(row, model_name, args):
    start_time = time()
    messages = generate_prompt(
        prompt_file=args.prompt_file[0],
        question=row["question"],
        db_name=row["db_name"],
        db_type=args.db_type,
        instructions=row["instructions"],
        k_shot_prompt=row["k_shot_prompt"],
        table_metadata_string=row["table_metadata_string"],
        public_data=not args.use_private_data,
        shuffle=args.shuffle_metadata,
    )
    print(messages)
    try:
        response = chat_openai(messages=messages, model=model_name, temperature=0.1,
	base_url="https://node4-api.staging.greenjello.io/v1"
        )
        generated_query = (
            response.content.split("```sql", 1)[-1].split("```", 1)[0].strip()
        )
        try:
            generated_query = sqlparse.format(
                generated_query, reindent=True, keyword_case="upper"
            )
        except:
            pass
        return {
            "query": generated_query,
            "reason": "",
            "err": "",
            "latency_seconds": time() - start_time,
            "tokens_used": response.input_tokens + response.output_tokens,
            "cost_in_cents": response.cost_in_cents,
        }
    except Exception as e:
        return {
            "query": "",
            "reason": "",
            "err": f"GENERATION ERROR: {str(e)}",
            "latency_seconds": time() - start_time,
            "tokens_used": 0,
            "cost_in_cents": None,
        }


def run_score_eval(args):
    #creating log
    os.makedirs("logs",exist_ok=True) 
    LOG_PATH= os.path.join("logs",f"{args.model.replace('/','_')}.log") if args.model else "logs/model.log"

    logging.basicConfig(
      filename=LOG_PATH,
      level = logging.INFO,
      format = "%(asctime)s - %(levelname)s - %(message)s" 
    )
    # get params from args
    questions_file_list = args.questions_file
    prompt_file_list = args.prompt_file
    output_file_list = args.output_file
    num_questions = args.num_questions
    k_shot = args.k_shot
    db_type = args.db_type
    cot_table_alias = args.cot_table_alias

    for questions_file, prompt_file, output_file in zip(
        questions_file_list, prompt_file_list, output_file_list
    ):
        print(f"Using prompt file {prompt_file}")
        print("Preparing questions...")
        print(
            f"Using {'all' if num_questions is None else num_questions} question(s) from {questions_file}"
        )
        question_query_df = prepare_questions_df(
            questions_file, db_type, num_questions, k_shot, cot_table_alias
        )
        input_rows = question_query_df.to_dict("records")
        output_rows = []
        with ThreadPoolExecutor(args.parallel_threads) as executor:
            futures = []
            for row in input_rows:
                generated_query_fut = executor.submit(
                    process_row,
                    row=row,
                    model_name=args.model,
                    args=args,
                )
                futures.append(generated_query_fut)

            total_tried = 0
            total_correct = 0
            for f in (pbar := tqdm(as_completed(futures), total=len(futures))):
                total_tried += 1
                i = futures.index(f)
                row = input_rows[i]
                result_dict = f.result()
                query_gen = result_dict["query"]
                reason = result_dict["reason"]
                err = result_dict["err"]
                # save custom metrics
                if "latency_seconds" in result_dict:
                    row["latency_seconds"] = result_dict["latency_seconds"]
                if "tokens_used" in result_dict:
                    row["tokens_used"] = result_dict["tokens_used"]
                if "cost_in_cents" in result_dict:
                    row["cost_in_cents"] = result_dict["cost_in_cents"]
                row["generated_query"] = query_gen
                row["reason"] = reason
                row["error_msg"] = err
                #exact_match= correct = 0
                # save failures into relevant columns in the dataframe
                if "GENERATION ERROR" in err:
                    row["error_query_gen"] = 1
                else:
                    expected_query = row["query"]
                    db_name = row["db_name"]
                    db_type = row["db_type"]
                    try:
                        exact_match, correct = compare_query_results(
                            query_gold=expected_query,
                            query_gen=query_gen,
                            db_name=db_name,
                            db_type=db_type,
                            question=row["question"],
                            query_category=row["query_category"],
                            db_creds=db_creds_all[db_type],
                        )
                        if correct:
                            total_correct += 1
                            row["correct"] = 1
                            row["error_msg"] = ""
                        else:
                            row["correct"] = 0
                            row["error_msg"] = "INCORRECT RESULTS"
                    except Exception as e:
                        row["correct"] = 0
                        row["error_db_exec"] = 1
                        row["error_msg"] = f"EXECUTION ERROR: {str(e)}"
                output_rows.append(row)
                pbar.set_description(
                    f"Accuracy: {round(total_correct/total_tried * 100, 2)}% ({total_correct}/{total_tried})"
                )
                log_interaction(
                        prompt=row["question"],
			            response="",
                        filtered_response=row["generated_query"],
                        golden_query=row["query"],
                        correct= row["correct"],
                        #exact_match=int(exact_match),
                        error_msg=row["error_msg"],
                        correct_sofar= f"Correct so far: {total_correct}/{total_tried} ({100*total_correct/total_tried:.2f}%)"
                       )

        # save results to csv
        output_df = pd.DataFrame(output_rows)
        output_df = output_df.sort_values(by=["db_name", "query_category", "question"])
        if "prompt" in output_df.columns:
            del output_df["prompt"]
        # get num rows, mean correct, mean error_db_exec for each query_category
        agg_stats = (
            output_df.groupby("query_category")
            .agg(
                num_rows=("db_name", "count"),
                mean_correct=("correct", "mean"),
                mean_error_db_exec=("error_db_exec", "mean"),
            )
            .reset_index()
        )
        print(agg_stats)
        # get directory of output_file and create if not exist
        output_dir = os.path.dirname(output_file)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        output_df.to_csv(output_file, index=False, float_format="%.2f")

        # get average rate of correct results
        avg_subset = output_df["correct"].sum() / len(output_df)
        print(f"Average correct rate: {avg_subset:.2f}")

        #results = output_df.to_dict("records")
        #with open(
        #    f"./eval-visualizer/public/{output_file.split('/')[-1].replace('.csv', '.json')}",
        #    "w",
        #) as f:
        #    json.dump(results, f)

        print("Total cost of evaluation (in cents): ", output_df["cost_in_cents"].sum())
