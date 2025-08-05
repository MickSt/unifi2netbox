import json
from dotenv import load_dotenv
from slugify import slugify
import os
import re
import sys
import requests
import warnings
import logging
import pynetbox
import ipaddress
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3.exceptions import InsecureRequestWarning
# Import the unifi module instead of defining the Unifi class
from unifi.unifi import Unifi
# Suppress only the InsecureRequestWarning
warnings.simplefilter("ignore", InsecureRequestWarning)

load_dotenv()
logger = logging.getLogger(__name__)

# Define threads for each layer
MAX_CONTROLLER_THREADS = 5  # Number of UniFi controllers to process concurrently
MAX_SITE_THREADS = 8  # Number of sites to process concurrently per controller
MAX_DEVICE_THREADS = 8  # Number of devices to process concurrently per site
MAX_THREADS = 8 # Define threads based on available system cores or default

def get_postable_fields(base_url, token, url_path):
    """
    Retrieves the POST-able fields for NetBox path.
    """
    url = f"{base_url}/api/{url_path}/"
    logger.debug(f"Retrieving POST-able fields from NetBox API: {url}")
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }
    response = requests.options(url, headers=headers, verify=False)
    response.raise_for_status()  # Raise an error if the response is not successful

    # Extract the available POST fields from the API schema
    fields = response.json().get("actions", {}).get("POST", {})
    logger.debug(f"Retrieved {len(fields)} POST-able fields from NetBox API")
    return fields

def load_site_mapping(config=None):
    """
    Load site mapping from configuration or YAML file.
    Returns a dictionary mapping UniFi site names to NetBox site names.
    
    :param config: Configuration dictionary loaded from config.yaml
    :return: Dictionary mapping UniFi site names to NetBox site names
    """
    # Initialize with empty mapping
    site_mapping = {}
    
    # First check if config has site mappings defined directly
    if config and 'UNIFI' in config and 'SITE_MAPPINGS' in config['UNIFI']:
        logger.debug("Loading site mappings from config.yaml")
        config_mappings = config['UNIFI']['SITE_MAPPINGS']
        if config_mappings:
            site_mapping.update(config_mappings)
            logger.debug(f"Loaded {len(config_mappings)} site mappings from config.yaml")
    
    # Check if we should use the external mapping file
    use_file_mapping = False
    if config and 'UNIFI' in config and 'USE_SITE_MAPPING' in config['UNIFI']:
        use_file_mapping = config['UNIFI']['USE_SITE_MAPPING']
        
    if use_file_mapping:
        site_mapping_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'site_mapping.yaml')
        logger.debug(f"Loading site mapping from file: {site_mapping_path}")
        
        # Check if file exists, if not create a default one
        if not os.path.exists(site_mapping_path):
            logger.warning(f"Site mapping file not found at {site_mapping_path}. Creating a default one.")
            os.makedirs(os.path.dirname(site_mapping_path), exist_ok=True)
            with open(site_mapping_path, 'w') as f:
                f.write("# Site mapping configuration\n")
                f.write("# Format: unifi_site_name: netbox_site_name\n")
                f.write("\"Default\": \"Default\"\n")
            
        try:
            with open(site_mapping_path, 'r') as f:
                file_mapping = yaml.safe_load(f) or {}
                logger.debug(f"Loaded {len(file_mapping)} mappings from site_mapping.yaml")
                # Update the mapping with file values (config values take precedence)
                for key, value in file_mapping.items():
                    if key not in site_mapping:  # Don't overwrite config mappings
                        site_mapping[key] = value
        except Exception as e:
            logger.error(f"Error loading site mapping file: {e}")
    
    logger.debug(f"Final site mapping has {len(site_mapping)} entries")
    return site_mapping

def get_netbox_site_name(unifi_site_name, config=None):
    """
    Get NetBox site name from UniFi site name using the mapping table.
    If no mapping exists, return the original name.
    
    :param unifi_site_name: The UniFi site name to look up
    :param config: Configuration dictionary loaded from config.yaml
    :return: The corresponding NetBox site name or the original name if no mapping exists
    """
    site_mapping = load_site_mapping(config)
    mapped_name = site_mapping.get(unifi_site_name, unifi_site_name)
    if mapped_name != unifi_site_name:
        logger.debug(f"Mapped UniFi site '{unifi_site_name}' to NetBox site '{mapped_name}'")
    return mapped_name

def prepare_netbox_sites(netbox_sites):
    """
    Pre-process NetBox sites for lookup.

    :param netbox_sites: List of NetBox site objects.
    :return: A dictionary mapping NetBox site names to the original NetBox site objects.
    """
    netbox_sites_dict = {}
    for netbox_site in netbox_sites:
        netbox_sites_dict[netbox_site.name] = netbox_site
    return netbox_sites_dict

def match_sites_to_netbox(ubiquity_desc, netbox_sites_dict, config=None):
    """
    Match Ubiquity site to NetBox site using the site mapping configuration.

    :param ubiquity_desc: The description of the Ubiquity site.
    :param netbox_sites_dict: A dictionary mapping NetBox site names to site objects.
    :param config: Configuration dictionary loaded from config.yaml
    :return: The matched NetBox site, or None if no match is found.
    """
    # Get the corresponding NetBox site name from the mapping
    netbox_site_name = get_netbox_site_name(ubiquity_desc, config)
    logger.debug(f'Mapping Ubiquity site: "{ubiquity_desc}" -> "{netbox_site_name}"')
    
    # Look for exact match in NetBox sites
    if netbox_site_name in netbox_sites_dict:
        netbox_site = netbox_sites_dict[netbox_site_name]
        logger.debug(f'Matched Ubiquity site "{ubiquity_desc}" to NetBox site "{netbox_site.name}"')
        return netbox_site
    
    # If site mapping is enabled but no match found, provide more helpful message
    if config and 'UNIFI' in config and ('USE_SITE_MAPPING' in config['UNIFI'] and config['UNIFI']['USE_SITE_MAPPING'] or 
                                        'SITE_MAPPINGS' in config['UNIFI'] and config['UNIFI']['SITE_MAPPINGS']):
        logger.debug(f'No match found for Ubiquity site "{ubiquity_desc}". Add mapping in config.yaml or site_mapping.yaml.')
    else:
        logger.debug(f'No match found for Ubiquity site "{ubiquity_desc}". Enable site mapping in config.yaml if needed.')
    return None

def setup_logging(min_log_level=logging.INFO):
    """
    Sets up logging to separate files for each log level.
    Only logs from the specified `min_log_level` and above are saved in their respective files.
    Includes console logging for the same log levels.

    :param min_log_level: Minimum log level to log. Defaults to logging.INFO.
    """
    logs_dir = "logs"
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)

    if not os.access(logs_dir, os.W_OK):
        raise PermissionError(f"Cannot write to log directory: {logs_dir}")

    # Log files for each level
    log_levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }

    # Create the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)  # Capture all log levels

    # Define a log format
    log_format = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # Set up file handlers for each log level
    for level_name, level_value in log_levels.items():
        if level_value >= min_log_level:
            log_file = os.path.join(logs_dir, f"{level_name.lower()}.log")
            handler = logging.FileHandler(log_file)
            handler.setLevel(level_value)
            handler.setFormatter(log_format)

            # Add a filter so only logs of this specific level are captured
            handler.addFilter(lambda record, lv=level_value: record.levelno == lv)
            logger.addHandler(handler)

    # Set up console handler for logs at `min_log_level` and above
    console_handler = logging.StreamHandler()
    console_handler.setLevel(min_log_level)
    console_handler.setFormatter(log_format)
    logger.addHandler(console_handler)

    logging.info(f"Logging is set up. Minimum log level: {logging.getLevelName(min_log_level)}")

def load_config(config_path="config/config.yaml"):
    """
    Reads the configuration from a YAML file.

    :param config_path: Path to the YAML configuration file.
    :return: A dictionary of the configuration.
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r") as file:
        try:
            config = yaml.safe_load(file)  # Use safe_load to avoid executing malicious YAML code
            return config
        except yaml.YAMLError as e:
            raise Exception(f"Error reading configuration file: {e}")

def process_device(unifi, nb, site, device, nb_ubiquity, tenant):
    """Process a device and add it to NetBox."""
    try:
        logger.info(f"Processing device {device['name']} at site {site}...")
        logger.debug(f"Device details: Model={device.get('model')}, MAC={device.get('mac')}, IP={device.get('ip')}, Serial={device.get('serial')}")

        # Determine device role
        if str(device.get("is_access_point", "false")).lower() == "true":
            nb_device_role = wireless_role
        else:
            nb_device_role = lan_role

        if not device.get("serial"):
            logger.warning(f"Missing serial number for device {device.get('name')}. Skipping...")
            return

        # VRF creation
        vrf_name = f"vrf_{site}"
        vrf = None
        logger.debug(f"Checking for existing VRF: {vrf_name}")
        try:
            vrf = nb.ipam.vrfs.get(name=vrf_name)
        except ValueError as e:
            error_message = str(e)
            if "get() returned more than one result." in error_message:
                logger.warning(f"Multiple VRFs with name {vrf_name} found. Using 1st one in the list.")
                vrfs = nb.ipam.vrfs.filter(name=vrf_name)
                for vrf_item in vrfs:
                    vrf = vrf_item
                    break
            else:
                logger.exception(f"Failed to get VRF {vrf_name} for site {site}: {e}. Skipping...")
                return

        if not vrf:
            logger.debug(f"VRF {vrf_name} not found, creating new VRF")
            vrf = nb.ipam.vrfs.create({"name": vrf_name})
            if vrf:
                logger.info(f"VRF {vrf_name} with ID {vrf.id} successfully added to NetBox.")

        # Device Type creation
        logger.debug(f"Checking for existing device type: {device['model']} (manufacturer ID: {nb_ubiquity.id})")
        nb_device_type = nb.dcim.device_types.get(model=device["model"], manufacturer_id=nb_ubiquity.id)
        if not nb_device_type:
            try:
                nb_device_type = nb.dcim.device_types.create({"manufacturer": nb_ubiquity.id, "model": device["model"],
                                                              "slug": slugify(f'{nb_ubiquity.name}-{device["model"]}')})
                if nb_device_type:
                    logger.info(f"Device type {device['model']} with ID {nb_device_type.id} successfully added to NetBox.")
            except pynetbox.core.query.RequestError as e:
                logger.error(f"Failed to create device type for {device['name']} at site {site}: {e}")
                return
            if len(device.get("port_table", [])) > 0:
                for port in device["port_table"]:
                    if port["media"] == "GE":
                        port_type = "1000base-t"
                        try:
                            template = nb.dcim.interface_templates.create({
                                "device_type": nb_device_type.id,
                                "name": port["name"],
                                "type": port_type,
                            })
                            if template:
                                logger.info(f"Interface template {port['name']} with ID {template.id} successfully added to NetBox.")
                        except pynetbox.core.query.RequestError as e:
                            logger.exception(f"Failed to create interface template for {device['name']} at site {site}: {e}")

        # Check for existing device
        logger.debug(f"Checking if device already exists: {device['name']} (serial: {device['serial']})")
        if nb.dcim.devices.get(site_id=site.id, serial=device["serial"]):
            logger.info(f"Device {device['name']} with serial {device['serial']} already exists. Skipping...")
            return

        # Create NetBox Device
        try:
            device_data = {
                    'name': device["name"],
                    'device_type': nb_device_type.id,
                    'tenant': tenant.id,
                    'site': site.id,
                    'serial': device["serial"]
                }

            logger.debug(f"Getting postable fields for NetBox API")
            available_fields = get_postable_fields(netbox_url, netbox_token, 'dcim/devices')
            logger.debug(f"Available NetBox API fields: {list(available_fields.keys())}")
            if 'role' in available_fields:
                logger.debug(f"Using 'role' field for device role (ID: {nb_device_role.id})")
                device_data['role'] = nb_device_role.id
            elif 'device_role' in available_fields:
                logger.debug(f"Using 'device_role' field for device role (ID: {nb_device_role.id})")
                device_data['device_role'] = nb_device_role.id
            else:
                logger.error(f'Could not determine the syntax for the role. Skipping device {device_name}, '
                                f'{serial}.')
                return None

            # Add the device to Netbox
            logger.debug(f"Creating device in NetBox with data: {device_data}")
            nb_device = nb.dcim.devices.create(device_data)

            if nb_device:
                logger.info(f"Device {device['name']} serial {device['serial']} with ID {nb_device.id} successfully added to NetBox.")
        except pynetbox.core.query.RequestError as e:
            error_message = str(e)
            if "Device name must be unique per site" in error_message:
                logger.warning(f"Device name {device['name']} already exists at site {site}. "
                               f"Trying with name {device['name']}_{device['serial']}.")
                try:
                    # Just update the name in the existing device_data dictionary
                    device_data['name'] = f"{device['name']}_{device['serial']}"
                    
                    # Add the device to Netbox with updated name
                    nb_device = nb.dcim.devices.create(device_data)
                    if nb_device:
                        logger.info(f"Device {device['name']} with ID {nb_device.id} successfully added to NetBox.")
                except pynetbox.core.query.RequestError as e2:
                    logger.exception(f"Failed to create device {device['name']} serial {device['serial']} at site {site}: {e2}")
                    return
            else:
                logger.exception(f"Failed to create device {device['name']} serial {device['serial']} at site {site}: {e}")
                return

        # Add primary IP if available
        try:
            ipaddress.ip_address(device["ip"])
        except ValueError:
            logger.warning(f"Invalid IP {device['ip']} for device {device['name']}. Skipping...")
            return
        # get the prefix that this IP address belongs to
        prefixes = nb.ipam.prefixes.filter(contains=device['ip'], vrf_id=vrf.id)
        if not prefixes:
            logger.warning(f"No prefix found for IP {device['ip']} for device {device['name']}. Skipping...")
            return
        for prefix in prefixes:
            # Extract the prefix length (mask) from the prefix
            subnet_mask = prefix.prefix.split('/')[1]
            ip = f'{device["ip"]}/{subnet_mask}'
            break
        if nb_device:
            interface = nb.dcim.interfaces.get(device_id=nb_device.id, name="vlan.1")
            if not interface:
                try:
                    interface = nb.dcim.interfaces.create(device=nb_device.id,
                                                          name="vlan.1",
                                                          type="virtual",
                                                          enabled=True,
                                                          vrf_id=vrf.id,)
                    if interface:
                        logger.info(
                            f"Interface vlan.1 for device {device['name']} with ID {interface.id} successfully added to NetBox.")
                except pynetbox.core.query.RequestError as e:
                    logger.exception(
                        f"Failed to create interface vlan.1 for device {device['name']} at site {site}: {e}")
                    return
            nb_ip = nb.ipam.ip_addresses.get(address=ip, vrf_id=vrf.id, tenant_id=tenant.id)
            if not nb_ip:
                try:
                    nb_ip = nb.ipam.ip_addresses.create({
                        "assigned_object_id": interface.id,
                        "assigned_object_type": 'dcim.interface',
                        "address": ip,
                        "vrf_id": vrf.id,
                        "tenant_id": tenant.id,
                        "status": "active",
                    })
                    if nb_ip:
                        logger.info(f"IP address {ip} with ID {nb_ip.id} successfully added to NetBox.")
                except pynetbox.core.query.RequestError as e:
                    logger.exception(f"Failed to create IP address {ip} for device {device['name']} at site {site}: {e}")
                    return
            if nb_ip:
                nb_device.primary_ip4 = nb_ip.id
                nb_device.save()
                logger.info(f"Device {device['name']} with IP {ip} added to NetBox.")

    except Exception as e:
        logger.exception(f"Failed to process device {device['name']} at site {site}: {e}")

def process_site(unifi, nb, site_name, nb_site, nb_ubiquity, tenant):
    """
    Process devices for a given site and add them to NetBox.
    """
    logger.debug(f"Processing site {site_name}...")
    try:
        logger.debug(f"Fetching site object for: {site_name}")
        site = unifi.site(site_name)
        if site:
            logger.debug(f"Fetching devices for site: {site_name}")
            devices = site.device.all()
            logger.debug(f"Found {len(devices)} devices for site {site_name}")

            with ThreadPoolExecutor(max_workers=MAX_DEVICE_THREADS) as executor:
                futures = []
                for device in devices:
                    futures.append(executor.submit(process_device, unifi, nb, nb_site, device, nb_ubiquity, tenant))

                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Error processing a device at site {site_name}: {e}")
        else:
            logger.error(f"Site {site_name} not found")
    except Exception as e:
        logger.error(f"Failed to process site {site_name}: {e}")

def process_controller(unifi_url, unifi_username, unifi_password, unifi_mfa_secret, nb, nb_ubiquity, tenant,
                       netbox_sites_dict, config=None):
    """
    Process all sites and devices for a specific UniFi controller.
    """
    logger.info(f"Processing controller {unifi_url}...")
    logger.debug(f"Initializing UniFi connection to: {unifi_url}")

    try:
        # Create a Unifi instance and authenticate
        unifi = Unifi(unifi_url, unifi_username, unifi_password, unifi_mfa_secret)
        logger.debug(f"UniFi connection established to: {unifi_url}")
        
        # Get all sites from the controller
        logger.debug(f"Fetching sites from controller: {unifi_url}")
        sites = unifi.sites
        logger.debug(f"Found {len(sites)} sites on controller: {unifi_url}")
        logger.info(f"Found {len(sites)} sites for controller {unifi_url}")

        with ThreadPoolExecutor(max_workers=MAX_SITE_THREADS) as executor:
            futures = []
            for site_name, site_obj in sites.items():
                logger.info(f"Processing site {site_name}...")
                nb_site = match_sites_to_netbox(site_name, netbox_sites_dict, config)

                if not nb_site:
                    logger.warning(f"No match found for Ubiquity site: {site_name}. Skipping...")
                    continue

                futures.append(executor.submit(process_site, unifi, nb, site_name, nb_site, nb_ubiquity, tenant))

            # Wait for all site-processing threads to complete
            for future in as_completed(futures):
                future.result()
    except Exception as e:
        logger.error(f"Error processing controller {unifi_url}: {e}")

def process_all_controllers(unifi_url_list, unifi_username, unifi_password, unifi_mfa_secret, nb, nb_ubiquity, tenant,
                            netbox_sites_dict, config=None):
    """
    Process all UniFi controllers in parallel.
    """
    with ThreadPoolExecutor(max_workers=MAX_CONTROLLER_THREADS) as executor:
        futures = []
        for url in unifi_url_list:
            futures.append(
                executor.submit(process_controller, url, unifi_username, unifi_password, unifi_mfa_secret, nb,
                                nb_ubiquity, tenant, netbox_sites_dict, config))

        # Wait for all controller-processing threads to complete
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.exception(f"Error processing one of the UniFi controllers {url}: {e}")
                continue

def fetch_site_devices(unifi, site_name):
    """Fetch devices for a specific site."""
    logger.info(f"Fetching devices for site {site_name}...")
    try:
        logger.debug(f"Getting site object for: {site_name}")
        site = unifi.site(site_name)
        if site:
            logger.debug(f"Retrieving devices for site: {site_name}")
            devices = site.device.all()
            logger.debug(f"Retrieved {len(devices)} devices for site: {site_name}")
            return devices
        else:
            logger.error(f"Site {site_name} not found")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch devices for site {site_name}: {e}")
        return None

def process_all_sites(unifi, netbox_sites_dict, nb, nb_ubiquity, tenant):
    """Process all sites and their devices concurrently."""
    # Get all sites from the unifi module
    unifi_sites = unifi.sites
    if not unifi_sites:
        logger.error("Failed to fetch sites from UniFi controller.")
        return

    sites = {}
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        # Fetch all devices per site concurrently
        future_to_site = {executor.submit(fetch_site_devices, unifi, site_name): site_name for site_name in unifi_sites.keys()}
        for future in as_completed(future_to_site):
            site_name = future_to_site[future]
            try:
                devices = future.result()
                if devices:
                    sites[site_name] = devices
                    logger.info(f"Successfully fetched devices for site {site_name}")
            except Exception as e:
                logger.error(f"Error fetching devices for site {site_name}: {e}")

    logger.info(f"Fetched {len(sites)} sites. Starting device processing...")

    # Process devices in parallel
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        future_to_device = {}
        for site_name, devices in sites.items():
            # Use the site mapping to find the corresponding NetBox site
            nb_site = match_sites_to_netbox(site_name, netbox_sites_dict)
            if not nb_site:
                logger.warning(f"No matching NetBox site found for Ubiquity site {site_name}. Add mapping in site_mapping.yaml. Skipping...")
                continue
            for device in devices:
                future = executor.submit(process_device, unifi, nb, nb_site, device, nb_ubiquity, tenant)
                future_to_device[future] = (site_name, device)

        for future in as_completed(future_to_device):
            site_name, device = future_to_device[future]
            try:
                future.result()
                logger.info(f"Successfully processed device {device['name']} at site {site_name}.")
            except Exception as e:
                logger.error(f"Error processing device {device['name']} at site {site_name}: {e}")

def parse_successful_log_entries(log_file):
    """
    Parses a log file to find entries containing 'successfully added to NetBox'
    and builds a dictionary with 'device' and 'ip address' lists of IDs.

    :param log_file: Path to the log file
    :return: Dictionary with lists of IDs for 'device' and 'ip address'
    """
    # Dictionary to store the resulting lists
    result = {
        "device": [],
        "ip address": []
    }

    # Regular expression to extract the ID from the log entry
    id_pattern_device = re.compile(r"^Device .* with ID (\d+) successfully added to NetBox")
    id_pattern_ip = re.compile(r"^IP address .* with ID (\d+) successfully added to NetBox")

    with open(log_file, "r") as file:
        for line in file:
            # Start processing the log entry only after `INFO -`
            if "INFO - " in line:
                log_content = line.split("INFO - ", 1)[1]  # Extract the part after "INFO - "

                # Match and classify the log entry
                if match := id_pattern_device.match(log_content):
                    result["device"].append(int(match.group(1)))  # Extract and add device ID
                elif match := id_pattern_ip.match(log_content):
                    result["ip address"].append(int(match.group(1)))  # Extract and add IP address ID

    return result


if __name__ == "__main__":
    # Parse command line arguments
    import argparse
    parser = argparse.ArgumentParser(description='Sync UniFi devices to NetBox')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose (debug) logging')
    args = parser.parse_args()
    
    # Configure logging with appropriate level based on verbose flag
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(log_level)
    
    if args.verbose:
        logger.debug("Verbose logging enabled")
    logger.debug("Loading configuration")
    config = load_config()
    logger.debug("Configuration loaded successfully")
    try:
        unifi_url_list = config['UNIFI']['URLS']
    except ValueError:
        logger.exception("Unifi URL is missing from configuration.")
        raise SystemExit(1)

    try:
        unifi_username = os.getenv('UNIFI_USERNAME')
        unifi_password = os.getenv('UNIFI_PASSWORD')
        unifi_mfa_secret = os.getenv('UNIFI_MFA_SECRET')
    except KeyError:
        logger.exception("Unifi username or password is missing from environment variables.")
        raise SystemExit(1)

    # Connect to Netbox
    try:
        netbox_url = config['NETBOX']['URL']
    except ValueError:
        logger.exception("Netbox URL is missing from configuration.")
        raise SystemExit(1)
    try:
        netbox_token = os.getenv('NETBOX_TOKEN')
    except KeyError:
        logger.exception("Netbox token is missing from environment variables.")
        raise SystemExit(1)

    # Create a custom HTTP session as this script will often exceed the default pool size of 10
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)

    # Adjust connection pool size
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    logger.debug(f"Initializing NetBox API connection to: {netbox_url}")
    nb = pynetbox.api(netbox_url, token=netbox_token, threading=True)
    nb.http_session.verify = False
    nb.http_session = session  # Attach the custom session
    logger.debug("NetBox API connection established")

    nb_ubiquity = nb.dcim.manufacturers.get(slug='ubiquity')
    try:
        tenant_name = config['NETBOX']['TENANT']
    except ValueError:
        logger.exception("Netbox tenant is missing from configuration.")
        raise SystemExit(1)

    tenant = nb.tenancy.tenants.get(name=tenant_name)

    try:
        wireless_role_name = config['NETBOX']['ROLES']['WIRELESS']
    except KeyError:
        logger.exception("Netbox wireless role is missing from configuration.")
        raise SystemExit(1)
    try:
        lan_role_name = config['NETBOX']['ROLES']['LAN']
    except KeyError:
        logger.exception("Netbox lan role is missing from configuration.")
        raise SystemExit(1)

    wireless_role = nb.dcim.device_roles.get(slug=wireless_role_name.lower())
    lan_role = nb.dcim.device_roles.get(slug=lan_role_name.lower())
    if not wireless_role:
        wireless_role = nb.dcim.device_roles.create({'name': wireless_role_name, 'slug': wireless_role_name.lower()})
        if wireless_role:
            logger.info(f"Wireless role {wireless_role_name} with ID {wireless_role.id} successfully added to Netbox.")
    if not lan_role:
        lan_role = nb.dcim.device_roles.create({'name': lan_role_name, 'slug': lan_role_name.lower()})
        if lan_role:
            logger.info(f"LAN role {lan_role_name} with ID {lan_role.id} successfully added to Netbox.")

    logger.debug("Fetching all NetBox sites")
    netbox_sites = nb.dcim.sites.all()
    logger.debug(f"Found {len(netbox_sites)} sites in NetBox")

    # Preprocess NetBox sites
    logger.debug("Preparing NetBox sites dictionary")
    netbox_sites_dict = prepare_netbox_sites(netbox_sites)
    logger.debug(f"Prepared {len(netbox_sites_dict)} NetBox sites for mapping")

    if not nb_ubiquity:
        nb_ubiquity = nb.dcim.manufacturers.create({'name': 'Ubiquity Networks', 'slug': 'ubiquity'})
        if nb_ubiquity:
            logger.info(f"Ubiquity manufacturer with ID {nb_ubiquity.id} successfully added to Netbox.")

    # Process all UniFi controllers in parallel
    process_all_controllers(unifi_url_list, unifi_username, unifi_password, unifi_mfa_secret, nb, nb_ubiquity,
                            tenant, netbox_sites_dict, config)
