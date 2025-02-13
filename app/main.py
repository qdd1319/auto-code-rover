"""
The main driver.
"""

import argparse
import datetime
import json
from collections.abc import Mapping
from multiprocessing import Pool
from os.path import join as pjoin

from app import globals, globals_mut, inference, log
from app import utils as apputils
from app.api.manage import ProjectApiManager
from app.fresh_issue.common import FreshTask
from app.post_process import (
    extract_organize_and_form_input,
    get_final_patch_path,
    organize_and_form_input,
    reextract_organize_and_form_inputs,
)


def parse_task_list_file(task_list_file: str) -> list[str]:
    """
    Parse the task list file.
    The file should contain one task/instance id per line, without other characters.
    """
    with open(task_list_file) as f:
        task_ids = f.readlines()
    return [x.strip() for x in task_ids]


class Task:
    """
    Encapsulate everything required to run one task.
    """

    def __init__(
        self, task_counter: str, task_id: str, setup_info: dict, task_info: dict
    ):
        # a counter str, format "1/150", which means first task out of 150
        self.task_counter = task_counter
        # id from the benchmark
        self.task_id = task_id
        # setup_info (Dict): keys: ['repo_path', 'env_name', 'pre_install', 'install','test_cmd']
        self.setup_info = setup_info
        # task_info (Dict): keys: ['base_commit', 'hints_text', 'created_at',
        # 'test_patch', 'repo', 'problem_statement', 'version', 'instance_id',
        # 'FAIL_TO_PASS', 'PASS_TO_PASS', 'environment_setup_commit']
        self.task_info = task_info


def run_one_task(task: Task) -> bool:
    """
    High-level entry for running one task.

    Args:
        - task: The Task instance to run.

    Returns:
        Whether the task completed successfully.
    """
    task_id = task.task_id
    setup_info = task.setup_info
    task_info = task.task_info
    repo_path = setup_info["repo_path"]
    env_name = setup_info["env_name"]
    pre_install_cmds = setup_info["pre_install"]
    install_cmd = setup_info["install"]
    # command to run the relevant tests
    test_cmd = setup_info["test_cmd"]
    base_commit = task_info["base_commit"]
    problem_stmt = task_info["problem_statement"]
    repo_name = task_info["repo"]
    # modifications to the test suite for this task instance
    test_patch = task_info["test_patch"]
    testcases_passing = task_info["PASS_TO_PASS"]
    testcases_failing = task_info["FAIL_TO_PASS"]

    # use time as part of folder name so it's always unique
    start_time = datetime.datetime.now()
    start_time_s = start_time.strftime("%Y-%m-%d_%H-%M-%S")
    task_output_dir = pjoin(globals.output_dir, task_id + "_" + start_time_s)
    apputils.create_dir_if_not_exists(task_output_dir)

    commit_hash = apputils.get_current_commit_hash()

    # save some meta data and other files for convenience
    meta = {
        "task_id": task_id,
        "setup_info": setup_info,
        "task_info": task_info,
    }
    with open(pjoin(task_output_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=4)
    with open(pjoin(task_output_dir, "problem_statement.txt"), "w") as f:
        f.write(problem_stmt)
    with open(pjoin(task_output_dir, "developer_patch.diff"), "w") as f:
        f.write(task_info["patch"])

    logger = log.create_new_logger(task_id, task_output_dir)
    log.log_and_always_print(
        logger,
        f"============= Running task {task_id} =============",
    )

    try:
        # create api manager and run project initialization routine in its init
        api_manager = ProjectApiManager(
            task_id,
            repo_path,
            base_commit,
            task_output_dir,
            env_name,
            repo_name,
            pre_install_cmds,
            install_cmd,
            test_cmd,
            test_patch,
            testcases_passing,
            testcases_failing,
            do_install=globals.do_install,
        )
    except Exception as e:
        log.log_exception(logger, e)
        run_status_message = (
            f"Task {task_id} failed with exception when creating API manager: {e}."
        )
        logger.handlers.clear()
        return False

    # special mode 2: only saving SBFL result
    if globals.only_save_sbfl_result:
        run_ok = (
            api_manager.fault_localization()
        )  # this should have saved the results into json
        if run_ok:
            log.log_and_always_print(
                logger, f"[SBFL only] Task {task_id} completed successfully."
            )
        else:
            log.log_and_always_print(
                logger, f"[SBFL only] Task {task_id} failed to produce result."
            )
        return True

    # run inference and catch error
    run_ok = False
    run_status_message = ""
    try:

        run_ok = inference.run_one_task(task_output_dir, api_manager, problem_stmt)
        if run_ok:
            run_status_message = f"Task {task_id} completed successfully."
        else:
            run_status_message = f"Task {task_id} failed without exception."
    except Exception as e:
        log.log_exception(logger, e)
        run_status_message = f"Task {task_id} failed with exception: {e}."
        run_ok = False
    finally:
        # dump recorded tool call sequence into a file
        end_time = datetime.datetime.now()

        api_manager.dump_tool_call_sequence_to_file()
        api_manager.dump_tool_call_layers_to_file()

        input_cost_per_token = globals.MODEL_COST_PER_INPUT[globals.model]
        output_cost_per_token = globals.MODEL_COST_PER_INPUT[globals.model]
        with open(pjoin(task_output_dir, "cost.json"), "w") as f:
            json.dump(
                {
                    "model": globals.model,
                    "commit": commit_hash,
                    "input_cost_per_token": input_cost_per_token,
                    "output_cost_per_token": output_cost_per_token,
                    "total_input_tokens": api_manager.input_tokens,
                    "total_output_tokens": api_manager.output_tokens,
                    "total_tokens": api_manager.input_tokens
                    + api_manager.output_tokens,
                    "total_cost": api_manager.cost,
                    "start_epoch": start_time.timestamp(),
                    "end_epoch": end_time.timestamp(),
                    "elapsed_seconds": (end_time - start_time).total_seconds(),
                },
                f,
                indent=4,
            )

        # at the end of each task, reset everything in the task repo to clean state
        with apputils.cd(repo_path):
            apputils.repo_reset_and_clean_checkout(base_commit, logger)
        log.log_and_always_print(logger, run_status_message)
        logger.handlers.clear()
        return run_ok


def run_task_group(task_group_id: str, task_group_items: list[Task]) -> None:
    """
    Run all tasks in a task group sequentially.
    Main entry to parallel processing.
    """
    log.print_with_time(
        f"Starting process for task group {task_group_id}. Number of tasks: {len(task_group_items)}."
    )
    for task in task_group_items:
        # within a group, the runs are always sequential
        run_one_task(task)
        log.print_with_time(globals_mut.incre_task_return_msg())

    log.print_with_time(
        f"{globals_mut.incre_task_group_return_msg()} Finished task group {task_group_id}."
    )


def entry_swe_bench_mode(
    task_id: str | None,
    task_list_file: str | None,
    setup_map_file: str,
    tasks_map_file: str,
    num_processes: int,
):
    """
    Main entry for swe-bench mode.
    """
    # check parameters
    if task_id is not None and task_list_file is not None:
        raise ValueError("Cannot specify both task and task-list.")

    all_task_ids = []
    if task_list_file is not None:
        all_task_ids = parse_task_list_file(task_list_file)
    if task_id is not None:
        all_task_ids = [task_id]
    if len(all_task_ids) == 0:
        raise ValueError("No task ids to run.")

    with open(setup_map_file) as f:
        setup_map = json.load(f)
    with open(tasks_map_file) as f:
        tasks_map = json.load(f)

    apputils.create_dir_if_not_exists(globals.output_dir)

    # Check if all task ids are in the setup and tasks map.
    missing_task_ids = [
        x for x in all_task_ids if not (x in setup_map and x in tasks_map)
    ]
    if missing_task_ids:
        # Log the tasks that are not in the setup or tasks map
        for task_id in sorted(missing_task_ids):
            log.print_with_time(
                f"Skipping task {task_id} which was not found in setup or tasks map."
            )
        # And drop them from the list of all task ids
        all_task_ids = filter(lambda x: x not in missing_task_ids, all_task_ids)

    all_task_ids = sorted(all_task_ids)
    num_tasks = len(all_task_ids)
    globals_mut.init_total_num_tasks(num_tasks)

    # for each task in the list to run, create a Task instance
    all_tasks = []
    for idx, task_id in enumerate(all_task_ids):
        setup_info = setup_map[task_id]
        task_info = tasks_map[task_id]
        task = Task(f"{idx + 1}/{num_tasks}", task_id, setup_info, task_info)
        all_tasks.append(task)

    # group tasks based on repo-version; tasks in one group should
    # be executed in one thread
    # key: env_name (a combination of repo+version), value: list of tasks
    task_groups: Mapping[str, list[Task]] = dict()
    task: Task
    for task in all_tasks:
        key = task.setup_info["env_name"]
        if key not in task_groups:
            task_groups[key] = []
        task_groups[key].append(task)

    # print some info about task
    log.print_with_time(f"Total number of tasks: {num_tasks}")
    log.print_with_time(f"Total number of processes: {num_processes}")
    log.print_with_time(f"Task group info: (number of groups: {len(task_groups)})")
    for key, tasks in task_groups.items():
        log.print_with_time(f"\t{key}: {len(tasks)} tasks")

    # single process mode
    if num_processes == 1:
        log.print_with_time("Running in single process mode.")
        for task in all_tasks:
            run_one_task(task)
        log.print_with_time("Finished all tasks sequentially.")

    # multi process mode
    else:
        # prepare for parallel processing
        num_task_groups = len(task_groups)
        globals_mut.init_total_num_task_groups(num_task_groups)
        num_processes = min(num_processes, num_task_groups)
        # If the function for Pool.map accepts multiple arguments, each argument should
        # be prepared in the form of a list for multiple processes.
        task_group_ids_items: list[tuple[str, list[Task]]] = list(task_groups.items())
        task_group_ids_items = sorted(
            task_group_ids_items, key=lambda x: len(x[1]), reverse=True
        )
        log.print_with_time(
            f"Sorted task groups: {[x[0] for x in task_group_ids_items]}"
        )
        try:
            pool = Pool(processes=num_processes)
            pool.starmap(run_task_group, task_group_ids_items)
            pool.close()
            pool.join()
        finally:
            log.print_with_time("Finishing all tasks in the pool.")

    if globals.only_save_sbfl_result:
        log.print_with_time("Only saving SBFL results. Exiting.")
        return

    # post-process completed experiments to get input file to SWE-bench
    log.print_with_time("Post-processing completed experiment results.")
    swe_input_file = organize_and_form_input(globals.output_dir)
    log.print_with_time("SWE-Bench input file created: " + swe_input_file)


def entry_fresh_issue_mode(
    task_id: str,
    clone_link: str,
    commit_hash: str,
    issue_link: str | None,
    setup_dir: str,
    local_repo: str,
    issue_file: str | None,
):
    """
    Main entry for fresh issue mode.
    """
    # let's decide whether we want the issue online, or from a local file
    if not ((issue_link is None) ^ (issue_file is None)):
        raise ValueError("Exactly one of issue-link or issue-file should be provided.")

    # these are used by both web and local modes
    start_time = datetime.datetime.now()
    start_time_s = start_time.strftime("%Y-%m-%d_%H-%M-%S")
    task_output_dir = pjoin(globals.output_dir, task_id + "_" + start_time_s)
    apputils.create_dir_if_not_exists(task_output_dir)

    if issue_link is not None:
        # online issue
        # create setup directory
        apputils.create_dir_if_not_exists(setup_dir)
        fresh_task = FreshTask.construct_from_online(
            task_id, task_output_dir, clone_link, commit_hash, issue_link, setup_dir
        )
    else:
        # local issue
        fresh_task = FreshTask.construct_from_local(
            task_id, task_output_dir, local_repo, issue_file
        )

    logger = log.create_new_logger(task_id, task_output_dir)
    log.log_and_always_print(
        logger,
        f"============= Running fresh issue {task_id} =============",
    )

    try:
        api_manager = ProjectApiManager(
            task_id,
            fresh_task.project_dir,
            fresh_task.commit_hash,
            fresh_task.task_output_dir,
        )
    except Exception as e:
        log.log_exception(logger, e)
        run_status_message = f"Fresh issue {task_id} failed with exception when creating API manager: {e}."
        return False

    run_ok = False
    run_status_message = ""
    try:
        run_ok = inference.run_one_task(
            task_output_dir, api_manager, fresh_task.problem_stmt
        )
        if run_ok:
            run_status_message = f"Fresh issue {task_id} completed successfully."
        else:
            run_status_message = f"Fresh issue {task_id} failed without exception."
    except Exception as e:
        log.log_exception(logger, e)
        run_status_message = f"Fresh issue {task_id} failed with exception: {e}."
    finally:
        # dump recorded tool call sequence into a file
        end_time = datetime.datetime.now()

        api_manager.dump_tool_call_sequence_to_file()
        api_manager.dump_tool_call_layers_to_file()

        input_cost_per_token = globals.MODEL_COST_PER_INPUT[globals.model]
        output_cost_per_token = globals.MODEL_COST_PER_INPUT[globals.model]
        acr_commit = apputils.get_current_commit_hash()
        with open(pjoin(task_output_dir, "cost.json"), "w") as f:
            json.dump(
                {
                    "model": globals.model,
                    "commit": acr_commit,
                    "input_cost_per_token": input_cost_per_token,
                    "output_cost_per_token": output_cost_per_token,
                    "total_input_tokens": api_manager.input_tokens,
                    "total_output_tokens": api_manager.output_tokens,
                    "total_tokens": api_manager.input_tokens
                    + api_manager.output_tokens,
                    "total_cost": api_manager.cost,
                    "start_epoch": start_time.timestamp(),
                    "end_epoch": end_time.timestamp(),
                    "elapsed_seconds": (end_time - start_time).total_seconds(),
                },
                f,
                indent=4,
            )

        # at the end of each task, reset everything in the task repo to clean state
        with apputils.cd(fresh_task.project_dir):
            apputils.repo_reset_and_clean_checkout(fresh_task.commit_hash, logger)
        log.log_and_always_print(logger, run_status_message)
        final_patch_path = get_final_patch_path(task_output_dir)
        if final_patch_path is not None:
            log.log_and_always_print(
                logger, f"Please find the generated patch at: {final_patch_path}"
            )
        else:
            log.log_and_always_print(
                logger, "No patch generated. You can try to run ACR again."
            )
        return run_ok


def main():
    parser = argparse.ArgumentParser()
    ## Common options
    # where to store run results
    parser.add_argument(
        "--mode",
        default="swe_bench",
        choices=["swe_bench", "fresh_issue"],
        help="Choose to run tasks in SWE-bench, or a fresh issue from the internet.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        help="Path to the directory that stores the run results.",
    )
    parser.add_argument(
        "--num-processes",
        type=str,
        default=1,
        help="Number of processes to run the tasks in parallel.",
    )
    parser.add_argument(
        "--no-print",
        action="store_true",
        default=False,
        help="Do not print most messages to stdout.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-3.5-turbo-0125",
        choices=globals.MODELS,
        help="The model to use. Currently only OpenAI models are supported.",
    )
    parser.add_argument(
        "--model-temperature",
        type=float,
        default=0.0,
        help="The model temperature to use, for OpenAI models.",
    )
    parser.add_argument(
        "--conv-round-limit",
        type=int,
        default=15,
        help="Conversation round limit for the main agent.",
    )
    parser.add_argument(
        "--extract-patches",
        type=str,
        help="Only extract patches from the raw results dir. Voids all other arguments if this is used.",
    )
    parser.add_argument(
        "--re-extract-patches",
        type=str,
        help="same as --extract-patches, except that individual dirs are moved out of their categories first",
    )
    parser.add_argument(
        "--enable-layered",
        action="store_true",
        default=True,
        help="Enable layered code search.",
    )

    swe_group = parser.add_argument_group(
        "swe_bench", description="Arguments for running on SWE-bench tasks."
    )
    ## task info when running instances in SWE-bench
    swe_group.add_argument(
        "--setup-map",
        type=str,
        help="Path to json file that contains the setup information of the projects.",
    )
    swe_group.add_argument(
        "--tasks-map",
        type=str,
        help="Path to json file that contains the tasks information.",
    )
    swe_group.add_argument(
        "--task-list-file",
        type=str,
        help="Path to the file that contains all tasks ids to be run.",
    )
    swe_group.add_argument("--task", type=str, help="Task id to be run.")
    ## Only support test-based options for SWE-bench tasks for now
    swe_group.add_argument(
        "--enable-sbfl", action="store_true", default=False, help="Enable SBFL."
    )
    swe_group.add_argument(
        "--enable-validation",
        action="store_true",
        default=False,
        help="Enable validation in our workflow.",
    )
    swe_group.add_argument(
        "--enable-angelic",
        action="store_true",
        default=False,
        help="(Experimental) Enable angelic debugging",
    )
    swe_group.add_argument(
        "--enable-perfect-angelic",
        action="store_true",
        default=False,
        help="(Experimental) Enable perfect angelic debugging; overrides --enable-angelic",
    )
    swe_group.add_argument(
        "--save-sbfl-result",
        action="store_true",
        default=False,
        help="Special mode to only save SBFL results for future runs.",
    )

    fresh_group = parser.add_argument_group(
        "fresh_issue",
        description="Arguments for running on fresh issues from the internet.",
    )
    ## task info when running on new issues from GitHub
    fresh_group.add_argument(
        "--fresh-task-id",
        type=str,
        help="Assign an id to the current fresh issue task.",
    )
    fresh_group.add_argument(
        "--commit-hash", type=str, help="[Fresh issue] The commit hash to checkout."
    )
    # (1) for cloning repo and using a remote issue link
    fresh_group.add_argument(
        "--clone-link",
        type=str,
        help="[Fresh issue] The link to the repository to clone.",
    )
    fresh_group.add_argument(
        "--issue-link", type=str, help="[Fresh issue] The link to the issue."
    )
    fresh_group.add_argument(
        "--setup-dir",
        type=str,
        help="[Fresh issue] The directory where repositories should be cloned to.",
    )
    # (2) for using a local repo and local issue file
    fresh_group.add_argument(
        "--local-repo",
        type=str,
        help="[Fresh issue] Path to a local copy of the targer repo.",
    )
    fresh_group.add_argument(
        "--issue-file", type=str, help="[Fresh issue] Path to a local issue file."
    )

    args = parser.parse_args()
    ## common options
    mode = args.mode
    globals.output_dir = args.output_dir
    if globals.output_dir is not None:
        globals.output_dir = apputils.convert_dir_to_absolute(globals.output_dir)
    num_processes: int = int(args.num_processes)
    # set whether brief or verbose log
    print_stdout: bool = not args.no_print
    log.print_stdout = print_stdout
    globals.model = args.model
    globals.model_temperature = args.model_temperature
    globals.conv_round_limit = args.conv_round_limit
    extract_patches: str | None = args.extract_patches
    re_extract_patches: str | None = args.re_extract_patches
    globals.enable_layered = args.enable_layered

    ## options for swe-bench mode
    setup_map_file = args.setup_map
    tasks_map_file = args.tasks_map
    task_list_file: str | None = args.task_list_file
    task_id: str | None = args.task
    globals.enable_sbfl = args.enable_sbfl
    globals.enable_validation = args.enable_validation
    globals.enable_angelic = args.enable_angelic
    globals.enable_perfect_angelic = args.enable_perfect_angelic
    globals.only_save_sbfl_result = args.save_sbfl_result

    ## options for fresh_issue mode
    fresh_task_id = args.fresh_task_id
    clone_link = args.clone_link
    commit_hash = args.commit_hash
    issue_link = args.issue_link
    setup_dir = args.setup_dir
    if setup_dir is not None:
        setup_dir = apputils.convert_dir_to_absolute(setup_dir)
    local_repo = args.local_repo
    if local_repo is not None:
        local_repo = apputils.convert_dir_to_absolute(local_repo)
    issue_file = args.issue_file
    if issue_file is not None:
        issue_file = apputils.convert_dir_to_absolute(issue_file)

    ## Firstly deal with special modes
    if globals.only_save_sbfl_result and extract_patches is not None:
        raise ValueError(
            "Cannot save SBFL result and extract patches at the same time."
        )

    # special mode 1: extract patch, for this we can early exit
    if re_extract_patches is not None:
        extract_patches = apputils.convert_dir_to_absolute(re_extract_patches)
        reextract_organize_and_form_inputs(re_extract_patches)
        return

    if extract_patches is not None:
        extract_patches = apputils.convert_dir_to_absolute(extract_patches)
        extract_organize_and_form_input(extract_patches)
        return

    # we do not do install for fresh issue now
    globals.do_install = (mode == "swe_bench") and (
        globals.enable_sbfl
        or globals.enable_validation
        or globals.only_save_sbfl_result
    )

    if mode == "swe_bench":
        entry_swe_bench_mode(
            task_id, task_list_file, setup_map_file, tasks_map_file, num_processes
        )
    else:
        entry_fresh_issue_mode(
            fresh_task_id,
            clone_link,
            commit_hash,
            issue_link,
            setup_dir,
            local_repo,
            issue_file,
        )


if __name__ == "__main__":
    main()
