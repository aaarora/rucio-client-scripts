import json
import re
from dotenv import load_dotenv
from os import environ
from os.path import join, dirname
import logging
from time import time, sleep

from sense.client.workflow_combined_api import WorkflowCombinedApi
from sense.client.discover_api import DiscoverApi

dotenv_path = join(dirname(__file__), '.env')
load_dotenv(dotenv_path)

PROFILE_UUID = environ.get("SENSE_PROFILE_UUID")

def good_response(response):
    return bool(response and not any("ERROR" in r for r in response))

def get_uri(rse_name, regex=".*?", full=False):
    logging.info(f"Getting URI for {rse_name}")
    discover_api = DiscoverApi()
    response = discover_api.discover_lookup_name_get(rse_name, search="NetworkAddress")
    if not good_response(response):
        raise ValueError(f"Discover query failed for {rse_name}")
    response = json.loads(response)
    if not response["results"]:
        raise ValueError(f"No results for '{rse_name}'")
    matched_results = [result for result in response["results"] if re.search(regex, result["name/tag/value"])]
    if len(matched_results) == 0:
        raise ValueError(f"No results matched '{regex}'")
    full_uri = matched_results[0]["resource"]
    if full:
        return full_uri
    root_uri = discover_api.discover_lookup_rooturi_get(full_uri)
    if not good_response(root_uri):
        raise ValueError(f"Discover query failed for {full_uri}")
    logging.info(f"Got URI: {root_uri} for {rse_name}")
    return root_uri

def stage_link(src_uri, dst_uri, src_ipv6, dst_ipv6, instance_uuid="", alias=""):
    logging.info("Staging new SENSE link")
    workflow_api = WorkflowCombinedApi()
    workflow_api.instance_new() if instance_uuid == "" else setattr(workflow_api, "si_uuid", instance_uuid)
    intent = {
        "service_profile_uuid": PROFILE_UUID,
        "queries": [
            {
                "ask": "edit",
                "options": [
                    {"data.connections[0].terminals[0].uri": src_uri},
                    {"data.connections[0].terminals[0].ipv6_prefix_list": src_ipv6},
                    {"data.connections[0].terminals[1].uri": dst_uri},
                    {"data.connections[0].terminals[1].ipv6_prefix_list": dst_ipv6},
                ]
            },
            {"ask": "maximum-bandwidth", "options": [{"name": "Connection 1"}]}
        ]
    }
    if alias:
        intent["alias"] = alias
    response = workflow_api.instance_create(json.dumps(intent))
    if not good_response(response):
        raise ValueError(f"SENSE query failed for {instance_uuid}")
    response = json.loads(response)
    logging.debug(f"Staging returned response {response}")
    for query in response["queries"]:
        if query["asked"] == "maximum-bandwidth":
            result = query["results"][0]
            if "bandwidth" not in result:
                raise ValueError(f"SENSE query failed for {instance_uuid}")
            return response["service_uuid"], float(result["bandwidth"])

def provision_link(instance_uuid, src_uri, dst_uri, src_ipv6, dst_ipv6, bandwidth, alias=""):
    logging.info(f"Provisioning staged {int(bandwidth) / 1000}G link with instance uuid {instance_uuid}")
    workflow_api = WorkflowCombinedApi()
    workflow_api.si_uuid = instance_uuid
    intent = {
        "service_profile_uuid": PROFILE_UUID,
        "queries": [
            {
                "ask": "edit",
                "options": [
                    {"data.connections[0].bandwidth.capacity": str(bandwidth)},
                    {"data.connections[0].terminals[0].uri": src_uri},
                    {"data.connections[0].terminals[0].ipv6_prefix_list": src_ipv6},
                    {"data.connections[0].terminals[1].uri": dst_uri},
                    {"data.connections[0].terminals[1].ipv6_prefix_list": dst_ipv6},
                ]
            }
        ]
    }
    if alias:
        intent["alias"] = alias
    response = workflow_api.instance_create(json.dumps(intent))
    if not good_response(response):
        raise ValueError(f"SENSE query failed for {instance_uuid}")
    response = json.loads(response)
    logging.debug(f"Provisioning got response {response}")
    workflow_api.instance_operate("provision", sync="true")

def delete_link(instance_uuid):
    logging.info(f"Deleting link for instance uuid {instance_uuid}")
    workflow_api = WorkflowCombinedApi()
    status = workflow_api.instance_get_status(si_uuid=instance_uuid)
    if "error" in status:
        raise ValueError(status)
    if not any(status.startswith(s) for s in ["CREATE", "REINSTATE", "MODIFY"]):
        raise ValueError(f"Cannot cancel an instance in status '{status}'")
    workflow_api.instance_operate("cancel", si_uuid=instance_uuid, sync="true", force=str("READY" not in status).lower())
    logging.info(f"Link status is now {status}")
    start = time()
    while ("CANCEL - READY" not in status):
        if time() - start > 120:
            raise Exception("Link Deletion Timed Out")
        sleep(10)
        status = workflow_api.instance_get_status(si_uuid=instance_uuid)
    workflow_api.instance_delete(si_uuid=instance_uuid)