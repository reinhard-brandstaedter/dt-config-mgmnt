import time, datetime, sys, os
import requests, json, yaml, urllib3
from requests.auth import HTTPBasicAuth
#import dnspython as dns
import dns.resolver
from IPy import IP
from fqdn import FQDN
import tldextract
from urllib.parse import urlencode
import redis
import logging
import random
from dtconfig.ConfigSet import DTEnvironmentConfig
from textwrap import wrap
import dtconfig.ConfigEntities as ConfigTypes
import copy


# LOG CONFIGURATION
FORMAT = '%(asctime)s:%(levelname)s:%(name)s:%(message)s'
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format=FORMAT)
logger = logging.getLogger("configmanager")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


configcache = redis.StrictRedis(host='configcache', port=6379, db=0, charset="utf-8", decode_responses=True)
server = "https://api.dy.natrace.it:8443"
apiuser = os.environ.get("DT_API_USER")
apipwd = os.environ.get("DT_API_PWD")
logger.info("User for DT API: {}".format(apiuser))
if not apiuser:
    sys.exit("No api user found (ensure env variable DT_API_USER is set) ... can't continue")
if not apipwd:
    sys.exit("No password for api user found (ensure env variable DT_API_PWD is set) ... can't continue")

stdConfig = DTEnvironmentConfig("/definitions/entities.yml")
internaldomains = ["ondemand","hybrishosting","ycs"]


'''
- collects (all, maybe only tagged ones) http services from a tenant
- verifies hostnames if they are external resolveable
- consolidates identical hostnames (e.g.  http and https) or (potentially) combines top level domains into one application
- creates applications and application detection rules
- pushed applications to tenant

create application IDs:
- convert tenantid to hex
- take last 12 digits of hex
- append four digits 0001-0010 depending on how many applications are already there

create applications:
- every domain gets a new application (named like domain)
- every application gets rules to include all domains.suffix

returns a dict:
key: clusterid::tenantid => [appentities]
'''
def createAppConfigEntitiesFromServices():
    keys = configcache.keys("*::services")
    appentities_map = {}
    
    for key in keys:
        appnames = {}
        services = configcache.smembers(key)
        filters = set()
        for service in services:
            dc = tldextract.extract(service)
            #logger.info("{} {} {}".format(dc.subdomain, dc.domain, dc.suffix))
            if dc.domain in appnames:
                filters = appnames[dc.domain]
            else:
                filters = set()
            
            if dc.domain not in internaldomains:
                #filters.add(dc.domain+"."+dc.suffix)
                filters.add(dc.domain)
                appnames.update({dc.domain:filters})
            
        logger.info("{} : {}".format(key,appnames))
        
        c_id = key.split("::")[0]
        t_id = key.split("::")[1]
        appentities = []
        for appname,patterns in appnames.items():
            stdApplications = stdConfig.getStandardWebApplications()
            for stdApplication in stdApplications:
                application = copy.deepcopy(stdApplication)
                #appid_prefix = "{:0>12}".format(t_id.encode("utf-8").hex()[-12:]).upper()
                #appid_suffix = "{:0>4}".format(appname.encode("utf-8").hex()[-4:]).upper()
                appid = stdConfig.getStdAppEntityID(t_id,appname)
                application.setID(appid)
                application.setName(appname)
                
                #create applicationdetectionrules and attach it to the app
                for filter in patterns:
                    rule = copy.deepcopy(stdConfig.getStandardApplicationDetectionRule())
                    rule.setFilter(pattern=filter,matchType="CONTAINS",matchTarget="DOMAIN")
                    application.addDetectionRule(rule)
                    logger.info(rule)
                
                appentities.append(application)
        
        appentities_map.update({c_id+"::"+t_id : appentities})    
          
    return appentities_map


'''
- create a set of Standard Applicaation dashboards for tenants
'''
def createAppDashboardEntitiesFromApps(applications):
    dashboardentities_map = {}

    for tenant,appentities in applications.items():
        parts = tenant.split("::")
        c_id = parts[0]
        t_id = parts[1]
        #logger.info("Application Dashboards to create for {}: {}".format(tenant,appentities))

        # The synthetic monitors API doesn't allow to PUT new monitors with a predefined ID,
        # only posts are allowed and the synthetic monitor's ID will be created, hence none of
        # the other standard approaches with generated IDs does work.
        # As a result we need to get the ev. previously generated monitors, match by name and get their ID to update or create a new one :-(
        monitors = getSyntheticMonitors(c_id, t_id)

        dashboardentities = []
        for app in appentities:
            stdDashboards = stdConfig.getStandardApplicationDashboards()
            for stdDashboard in stdDashboards:
                dashboard = copy.deepcopy(stdDashboard)
                dashboard.setName(dashboard.getName() + " - " + app.getName())
                dashboard.setAssignedApplicationEntity(app.getID())
                if app.getName() in monitors:
                    dashboard.setAssignedSyntheticMonitorEntity(monitors[app.getName()])
                else:
                    logger.warning("Synthetic Monitor for {} not (yet) found, creating dashboard without synthetic monitor reference".format(app.getName()))

                # ensure the ID of the dashboard is unique and reflects the application it belongs to
                # replacing the original ID with a generated HEX fromt he application ID that has been generated before
                # e.g:
                # gggg0001-0a0a-0b0b-0c0c-000000000001 => gggg0001-6E76-692D-7031-737400000001
                prefix = dashboard.getID().split('-',1)[0]
                dbid = wrap(stdConfig.getStdAppEntityID(t_id,app.getName()),4)
                #dbid = wrap(app.getID().rsplit('-')[1],4)
                postfix = "{:0>8}".format(1)
                dbid[3] = dbid[3]+postfix
                id = [prefix]
                id.extend(dbid)
                newid = "-".join(id)

                dashboard.setID(newid)
                logger.info("Application Dashboard created: {}".format(dashboard))
                dashboardentities.append(dashboard)
        
        dashboardentities_map.update({c_id+"::"+t_id : dashboardentities})

    return dashboardentities_map

def putAppDashboards(dashboards):
    for tenant,dashboardentities in dashboards.items():
        parts = tenant.split("::")
        c_id = parts[0]
        t_id = parts[1]
        parameters = {"tenantid":t_id, "clusterid":c_id}
        #logger.info("Application Dashboards to create for {}: {}".format(tenant,dashboardentities))
        for dashboard in dashboardentities:
            logger.info("PUT Dashboard for {} : {}".format(parameters,dashboard))
        
        putConfigEntities(dashboardentities, parameters)

def getSyntheticMonitors(clusterid, tenantid):
    apiurl = "/e/TENANTID/api/v1/synthetic/monitors"
    parameters = {"clusterid":clusterid, "tenantid":tenantid}
    query = "?"+urlencode(parameters)
    url = server + apiurl + query
    
    try:
        response = requests.get(url, auth=(apiuser, apipwd))
        result = response.json()
        monitors = {}
        for tenant in result:
            for monitor in tenant["monitors"]:
                monitors.update({monitor["name"]:monitor["entityId"]})
    except:
        logger.error("Error while trying to get synthetic monitors for {}::{}".format(clusterid,tenantid))

    return monitors
    

'''
- creates a basic http synthetic monitor for every application
'''
def createSyntheticMonitorsFromApps(applications):
    monitorentities_map = {}

    for tenant,appentities in applications.items():
        parts = tenant.split("::")
        c_id = parts[0]
        t_id = parts[1]
        
        # The synthetic monitors API doesn't allow to PUT new monitors with a predefined ID,
        # only posts are allowed and the synthetic monitor's ID will be created, hence none of
        # the other standard approaches with generated IDs does work.
        # As a result we need to get the ev. previously generated monitors, match by name and get their ID to update or create a new one :-(
        monitors = getSyntheticMonitors(c_id, t_id)

        monitorentities = []
        key = "::".join([c_id, t_id, "services"])
        for app in appentities:
            appname = app.getName()
            # there are some internal domains that we do not want to create monitors for
            if appname not in internaldomains:
                monitor = copy.deepcopy(stdConfig.getStandardSyntheticMonitor())
                monitor.setName(appname)

                if appname in monitors:
                    monitorid = monitors[appname]
                    monitor.setID(monitorid.split("-")[1])
                else:
                    monitor.setID("")

                #monitor.setID(stdConfig.getStdAppEntityID(t_id,appname))
                
                monitor.setManuallyAssignedApps(app.getID())
                # since Dynatrace has no autotagging for monitors set one directly
                # this is not perfect as CCv2 has different cld-external-ids that can't be easily created from tenant ID
                monitor.setTags(["cld-external-id:"+t_id])

                # need to get homepage url from services
                # services of this tenant can be found in a set in cache: c_id::t_id::services
                url = "unknown"
                services = configcache.smembers(key)
                for service in services:
                    dc = tldextract.extract(service)
                    if dc.subdomain not in ["","shop","www","store","commerce"]:
                        logger.warn("No suitable Subdomain found in services, not configuring monitor for {}".format(".".join(dc)))
                        continue
                    # in case of multiple domains we just pick first one
                    if appname == dc.domain:
                        url = "https://"+(".".join(dc).strip("."))

                monitor.setHomepageUrl(url)
                monitorentities.append(monitor)
        
        monitorentities_map.update({c_id+"::"+t_id : monitorentities})

    return monitorentities_map

def putSyntheticMonitors(monitors):
    for tenant,monitorentities in monitors.items():
        parts = tenant.split("::")
        c_id = parts[0]
        t_id = parts[1]
        parameters = {"tenantid":t_id, "clusterid":c_id}
        #logger.info("Synthetic Monitors to create for {}: {}".format(tenant,monitorentities))
        new_monitorentities = []
        existing_monitorentities = []
        for monitor in monitorentities:
            if monitor.dto["entityId"] == "":
                new_monitorentities.append(monitor)
                logger.info("POST Monitor for {} : {}".format(parameters,monitor))
            else:
                existing_monitorentities.append(monitor)
                logger.info("PUT Monitor for {} : {}".format(parameters,monitor))
            
        postConfigEntities(new_monitorentities, parameters)
        putConfigEntities(existing_monitorentities, parameters)

        # since other configs might depend on the monitors to be available test if all have been applied successfully
        tries = complete = 0
        while complete < len(monitorentities) and tries < 15:
            monitors = getSyntheticMonitors(c_id,t_id)
            tries += 1
            for monitor in monitorentities:
                if monitor.getName() in monitors:
                    logger.info("Synthetic Monitor Deployed: {}".format(monitor.getName()))
                    complete += 1
                else:
                    logger.info("Synthetic Monitor Missing: {}".format(monitor.getName()))
            time.sleep(2)



def getApplicationNames():
    keys = configcache.keys("*::services")
    
    for key in keys:
        patterns = set()
        services = configcache.smembers(key)
        for service in services:
            dc = tldextract.extract(service)
            patterns.add(dc.domain+"."+dc.suffix)
            #logger.info("{} {} {}".format(dc.subdomain, dc.domain, dc.suffix))
        logger.info("{} : {}".format(key,patterns))

'''
returns if a detected service name is actually a public resolvable hostname/url
'''
def isPublicWebService(service):
    resolver = dns.resolver.Resolver()
    resolver.nameservers = ["8.8.8.8","8.8.4.4"]
    try:
        answer = resolver.query(service)
        ispublic = False
        for ipval in answer:
            ip = IP(ipval.to_text())
            ispublic = ispublic or ("PUBLIC" == ip.iptype())
            logger.info('{}: {} (Public: {})'.format(service,ipval.to_text(),ispublic))
        return ispublic
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except:
        logger.error("Couldn't resolve {}, problem with DNS service".format(service))
    
    return False
    

'''
Get all autodetected webservices from tenants and store their names in the redis cache as sets per tenant
'''
def getServices(parameters):
    apiurl = "/e/TENANTID/api/v1/entity/services"
    query = "?"+urlencode(parameters)
    url = server + apiurl + query
    
    try:
        response = requests.get(url, auth=(apiuser, apipwd))
        result = response.json()
        for tenant in result:
            c_id = tenant["clusterid"]
            t_id = tenant["tenantid"]
            key = "::".join([c_id, t_id, "services"])
            services = []
            for service in tenant["result"]:
                #strip any detected port from the service
                svc = service["discoveredName"].split(":")[0]
                #if there is a customizedName it overrides the discoveredName - useful for CCv2 where there sometimes is no proper vhost set on webservers
                if "customizedName" in service:
                    svc = service["customizedName"].split(":")[0] 
                # only keep services on port 80 or 443 (other ones are internal)
                svcport = service["discoveredName"].split(":")
                port = ""
                if len(svcport) > 1:
                    port = svcport[1]
                if port in ["443"]:
                    #ensure the service is a fqdn hostname
                    fqdn = False
                    try:
                        fqdn = FQDN(svc)
                        # to avoid duplicate lookups(DNS traffic)
                        if fqdn.is_valid and configcache.sismember(key,svc):
                            #logger.info("Found service {} on {} in cache, not querying".format(svc,key))
                            pass
                        else:
                            if fqdn.is_valid and "webServerName" in service and isPublicWebService(svc):
                                #logger.info("Service: {} is public".format(service["discoveredName"])))
                                configcache.sadd(key,svc)
                    except:
                        logger.warning("Exception happened: {}".format(sys.exc_info()))
                        continue
                    
            configcache.expire(key,600)
    except:
        logger.error("Problem Getting Services: {}".format(sys.exc_info()))


'''
Get all configured applications from tenants and store their IDs in the redis cache as sets per tenant
'''
def getApplications(parameters):
    apiurl = "/e/TENANTID/api/config/v1/applications/web"
    query = "?"+urlencode(parameters)
    url = server + apiurl + query
    
    try:
        response = requests.get(url, auth=(apiuser, apipwd))
        result = response.json()
        for tenant in result:
            c_id = tenant["clusterid"]
            t_id = tenant["tenantid"]
            key = "::".join([c_id, t_id, "applications"])
            # we want application IDs to be recognizable for the standard (what has been created through automation)
            # so we format them accordingly
            std_appid = "{:0>12}".format(t_id.encode("utf-8").hex()[-12:]).upper()
            applications = []
            for application in tenant["values"]:
                a_id = application["id"].split("-")[1]
                
                if a_id.startswith(std_appid):
                    logger.info("Application in standard ({}): {} {} : {}".format(std_appid, key, application["name"], application["id"]))
                    try:
                        configcache.sadd(key,std_appid)
                    except:
                        logger.warning("Exception happened: {}".format(sys.exc_info()))
                else:
                    logger.info("Application not in standard ({}): {} {} : {}".format(std_appid, key, application["name"], application["id"]))
                    
            configcache.expire(key,600)
    except:
        logger.error("Problem Getting Applications: {}".format(sys.exc_info()))


'''
writes Application Configurations (applications and detections rules) to tenants.
This is ALWAYS a tenant specific operation and NEVER a global one, so we make sure to pass the tenant parameter.
'''
def putApplicationConfigs(applications):
    ruleentities = []
    for tenant,appentities in applications.items():
        parts = tenant.split("::")
        c_id = parts[0]
        t_id = parts[1]
        parameters = {"tenantid":t_id, "clusterid":c_id}
        logger.info("Applications to create for {}: {}".format(tenant,appentities))
        ruleentities = []
        for app in appentities:
            ruleentities.extend(app.getDetectionRules())
            if len(ruleentities) > 250:
                logger.warning("More than 250 application detection rules would be created for {}".format(tenant))
            #logger.info(json.dumps(app.dto))
        
        logger.info("PUT Applications for {} : {}".format(parameters,appentities))
        putConfigEntities(appentities, parameters)
        #logger.info("PUT Application detection rules for {} : {}".format(parameters,ruleentities))
        putConfigEntities(ruleentities, parameters)


'''
Get all tenant's standard config definitions (by name) and store their IDs in cache.
This is required to get a current state of existing tenants and their config, required for further cleanup

Caches:
clusterid::tenantid::entitytype::entityname => id | missing
'''
def getConfigSettings(entitytypes, parameters):
    query = "?"+urlencode(parameters)
    
    for entitytype in entitytypes:
        apiurl = entitytype.uri
        url = server + apiurl + query
        stdConfigNames = stdConfig.getConfigEntitiesNamesByType(entitytype)
        configtype = entitytype.__name__
        logger.info("Getting configs of type: {}".format(entitytype.__name__))
            
        try:
            response = requests.get(url, auth=(apiuser, apipwd))
            result = response.json()
            for tenant in result:
                c_id = tenant["clusterid"]
                t_id = tenant["tenantid"]
                attrcheck = set()
                try:
                    attrkey = None
                    if "values" in tenant:
                        attrkey = "values"
                    if "dashboards" in tenant:
                        attrkey = "dashboards"
                    
                    if attrkey:
                        for attr in tenant[attrkey]:
                            #key = "::".join([c_id, t_id])
                            key = "::".join([c_id, t_id, configtype, attr["name"]])
                            #logger.info("Found: {}".format(key))
                            if "name" in attr and attr["name"] in stdConfigNames:
                                #logger.info("{} {} : {}".format(key,attr["name"], attr["id"]))
                                configcache.setex(key,3600,attr["id"])
                                attrcheck.add(attr["name"])
                            else:
                                configcache.setex(key,3600,attr["id"])
                                logger.info("{} not in standard: {} : {}".format(configtype, key, attr["id"]))
                    else:
                        logger.info("{} type is not a config type which returns a list of entities, it's a setting type - comparison not implemented yet".format(configtype))
                        logger.debug("JSON: {}".format(tenant))
                        centity = entitytype(id="",name="",dto=tenant)
                        logger.info(centity.dto)
                        #ToDo: get all 1st level attributes not added by the consolidation API but by DT
                        # iterate through those attributes and find id or namme attributes and if not then it's a setting (eg. dataPrivacy and not a config item)
                        # for those that are setting types it might make sense to compare the settings to the default for match
                except:
                    logger.error("Problem getting config of type: {} for Tenant {}::{}".format(configtype,c_id,t_id))
                    #logger.error("JSON: {}".format(result))
                    logger.error("Error: {}".format(sys.exc_info()))
                    continue
                
                # check if all entities of the standard have been found        
                if len(attrcheck) != len(stdConfigNames):
                    missing = set(set(stdConfigNames) - attrcheck)
                    logger.info("Missing {} on {} {}".format(configtype,"::".join([c_id, t_id]),missing))
                    for attr in missing:
                        key = "::".join([c_id, t_id, configtype, attr])
                        configcache.setex(key,3600,"missing")
               
        except:
            logger.error("Problem Getting Config Settings: {}".format(sys.exc_info()))


'''
updates or creates config entities with taking care of if the entity already exists with a different ID.
This method goes one by one for each tenant based on the information found already active.

It's using the information from the configcache that has been populated before with the config entities and their ID or if they are missing.
By going one by one tenant this is not a very effective method, but it's required for a initial "cleanup"of all tenants and establishing the standard
'''    
def updateOrCreateConfigEntities(entities, parameters):
    headers = {"Content-Type" : "application/json"}
    query = "?"+urlencode(parameters)
    
    missing = unmatched = matched = 0
    
    for entity in entities:
        configtype = type(entity).__name__
        configs = configcache.keys("*::"+configtype+"::"+entity.name)
        
        for key in configs:
            parts = key.split("::")
            tenantid = parts[1]
            try:
                stdID = entity.id
                curID = configcache.get(key)
            except:
                continue
            
            if "missing" == curID:
                logger.info("Standard {} {} is missing on {}".format(configtype,entity.name,tenantid))
                putConfigEntities([entity],{"tenantid":tenantid})
                missing += 1
                continue
            if stdID != curID:
                logger.info("Standard {} IDs for {} don't match on {}: {} : {}".format(configtype,entity.name,tenantid,stdID,curID))
                delEntity = copy.deepcopy(entity)
                delEntity.setID(curID)
                deleteConfigEntities([delEntity],{"tenantid":tenantid})
                putConfigEntities([entity],{"tenantid":tenantid})
                unmatched += 1
            else:
                #logger.info("Standard RequestAttribute IDs match, no action needed")
                matched += 1
    
    logger.info("Standard Config Entities: missing(added): {} unmatched(updated): {} matched: {}".format(missing,unmatched,matched))


'''
Purging of non-standard Config Entitytypes.

Use with caution, this will remove all settings that are not in the standard of the specified entitytypes
'''
def purgeConfigEntities(entitytypes,parameters,force):
    headers = {"Content-Type" : "application/json"}
    query = "?"+urlencode(parameters)
    #logger.info("Entities: {}".format(entitytypes))
    
    purged = 0
    
    for entitytype in entitytypes:
        configtype = entitytype.__name__
        logger.info("Purging Config of Type: {}".format(configtype))
        configs = configcache.keys("*::"+configtype+"::*")
        stdConfigNames = stdConfig.getConfigEntitiesNamesByType(entitytype)
        stdConfigIDs = stdConfig.getConfigEntitiesIDsByType(entitytype)
        stdConfigIDsShort = list(map(lambda id: id.rsplit("-",1)[0],stdConfigIDs))
        #logger.info(stdConfigIDsShort)
        
        for key in configs:
            parts = key.split("::")
            tenantid = parts[1]
            configname = key.split("::")[-1]
            
            try:
                configid = configcache.get(key)
                logger.info("Checking to purge: {} {}".format(configname,configid))
            
                # only purge the config if it's not within our own standard (do not purge old versions of our own standard)
                if force or (configname not in stdConfigNames) or (configid not in stdConfigIDs):
                    # check if config is maybe an older version
                    if configid.rsplit("-",1)[0] in stdConfigIDsShort:
                        logger.info("This seems to be one of ours (not purging unless forced): {}".format(configid))
                        if force:
                            purgeEntity = entitytype(id=configid,name=configname)
                            logger.warning("Forced purge of standard {} configuration on {}: {}".format(configtype, tenantid, purgeEntity))
                            deleteConfigEntities([purgeEntity],{"tenantid":tenantid})
                            purged += 1
                    else:
                        #create an instance of the entitytype
                        purgeEntity = entitytype(id=configid,name=configname)
                        logger.warning("Non-standard {} configuration on {} will be purged: {}".format(configtype, tenantid, purgeEntity))
                        deleteConfigEntities([purgeEntity],{"tenantid":tenantid})
                        purged += 1
            except:
                logger.error("Problem purging {} on {}: {}".format(configtype,tenantid,sys.exc_info()))
        
        if purged > 0:
            logger.info("Purged non-standard {} Entities: {}".format(configtype,purged))

def deleteConfigEntities(entities,parameters):
    query = "?"+urlencode(parameters)
    
    for entity in entities:
        status = {"204":0, "201":0, "400":0, "401":0, "404":0}
        url = server + entity.apipath + query
        configtype = type(entity).__name__
        logger.info("DELETE {}: {}".format(configtype,url))
        
        try:   
            resp = requests.delete(url, auth=(apiuser, apipwd))
            if len(resp.content) > 0:
                for tenant in resp.json():
                    status.update({str(tenant["responsecode"]):status[str(tenant["responsecode"])]+1})
                    if tenant["responsecode"] >= 400:
                        logger.info("tenant: {} status: {}".format(tenant["tenantid"], tenant["responsecode"]))
                logger.info("Status Summary: {} {}".format(len(resp.json()),status))
        except:
            logger.error("Problem deleting {}: {}".format(configtype,sys.exc_info()))
            

def putConfigEntities(entities,parameters):
    headers = {"Content-Type" : "application/json"}
    query = "?"+urlencode(parameters)
    
    for entity in entities:
        status = {"200":0, "204":0, "201":0, "400":0, "401":0, "404":0}
        url = server + entity.apipath + query
        configtype = type(entity).__name__
        logger.info("PUT {}: {}".format(configtype,url))
        
        try:   
            resp = requests.put(url,json=entity.dto, auth=(apiuser, apipwd))
            if len(resp.content) > 0:
                for tenant in resp.json():
                    status.update({str(tenant["responsecode"]):status[str(tenant["responsecode"])]+1})
                    if tenant["responsecode"] >= 400:
                        logger.info("tenant: {} status: {}".format(tenant["tenantid"], tenant["responsecode"]))
                        logger.error("PUT Payload: {}".format(json.dumps(entity.dto)))
                        logger.error("PUT Response: {}".format(json.dumps(tenant)))
                logger.info("Status Summary: {} {}".format(len(resp.json()),status))
        except:
            logger.error("Problem putting {}: {}".format(configtype,sys.exc_info()))

    # add additional verification that entities have been created?


def postConfigEntities(entities,parameters):
    headers = {"Content-Type" : "application/json"}
    query = "?"+urlencode(parameters)
    
    for entity in entities:
        status = {"200":0, "204":0, "201":0, "400":0, "401":0, "404":0}
        url = server + entity.uri + query
        configtype = type(entity).__name__
        logger.info("POST {}: {}".format(configtype,url))
        
        try:   
            resp = requests.post(url,json=entity.dto, auth=(apiuser, apipwd))
            if len(resp.content) > 0:
                for tenant in resp.json():
                    status.update({str(tenant["responsecode"]):status[str(tenant["responsecode"])]+1})
                    if tenant["responsecode"] >= 400:
                        logger.info("tenant: {} status: {}".format(tenant["tenantid"], tenant["responsecode"]))
                        logger.error("POST Payload: {}".format(json.dumps(entity.dto)))
                        logger.error("POST Response: {}".format(json.dumps(tenant)))
                logger.info("Status Summary: {} {}".format(len(resp.json()),status))
        except:
            logger.error("Problem putting {}: {}".format(configtype,sys.exc_info()))

def getControlSettings():

    stdSettings = {
        "servicerequestAttributes": True,
        "servicerequestNaming": True,
        "autoTags": True,
        "customServicesjava": True,
        "calculatedMetricsservice": True,
        "anomalyDetectionapplications": True,
        "anomalyDetectionservices": True,
        "applicationsweb": False,
        "applicationDetectionRules": False,
        "alertingProfiles": False,
        "notifications": False,
        "dataPrivacy": True, 
        "dashboards": True,
        "syntheticMonitors": False,
        "applicationDashboards": False,
        "dryrun": True
    }

    ctrlsettings = {}
    try:
        if configcache.exists("config") == 1:
            settings = configcache.get("config")
            ctrlsettings = json.loads(settings)
    except:
        logger.error("Problem reading proper config from redis, continueing with default settings. Error: {}".format(sys.exc_info()))

    #merge stdsetting with provided ones
    return {**stdSettings, **ctrlsettings}

def performConfig(parameters):
    logger.info("Configuration Parameters: {}".format(parameters))
    config = getControlSettings()
    logger.info("Applying Configuration Types: {}".format(config))

    if config["servicerequestAttributes"]:
        logger.info("++++++++ REQUEST ATTRIBUTES ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: servicerequestAttributes")
        else:
            # requestAttributes
            #purgeConfigEntities([ConfigTypes.servicerequestAttributes], parameters)
            updateOrCreateConfigEntities(stdConfig.getRequestAttributes(),parameters)
            putConfigEntities(stdConfig.getRequestAttributes(),parameters)
            
    
    if config["autoTags"]:
        logger.info("++++++++ AUTO TAGS ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: autoTags")
        else:
            # autoTags
            #purgeConfigEntities([ConfigTypes.autoTags], parameters)
            updateOrCreateConfigEntities(stdConfig.getAutoTags(),parameters)
            putConfigEntities(stdConfig.getAutoTags(),parameters)
    
    if config["customServicesjava"]:
        logger.info("++++++++ CUSTOM SERVICES ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: customServicesjava")
        else:
            # customServices
            #purgeConfigEntities([ConfigTypes.customServicesjava], parameters)
            updateOrCreateConfigEntities(stdConfig.getCustomJavaServices(),parameters)
            putConfigEntities(stdConfig.getCustomJavaServices(),parameters)
    
    if config["servicerequestNaming"]:
        logger.info("++++++++ REQUEST NAMING ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: servicerequestNaming")
        else:
            # requestNaming
            #purgeConfigEntities([ConfigTypes.servicerequestNaming], parameters)
            updateOrCreateConfigEntities(stdConfig.getRequestNamings(),parameters)
            putConfigEntities(stdConfig.getRequestNamings(),parameters)

    if config["calculatedMetricsservice"]:
        logger.info("++++++++ CUSTOM METRICS ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: calculatedMetricsservice")
        else:
            # customMetrics
            #purgeConfigEntities([ConfigTypes.customMetricservice], parameters, True)
            updateOrCreateConfigEntities(stdConfig.getCalculatedMetricsService(),parameters)
            putConfigEntities(stdConfig.getCalculatedMetricsService(),parameters)
        
    if config["dataPrivacy"]:
        logger.info("++++++++ DATA PRIVACY ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: dataPrivacy")
        else:
            # dataPrivacy
            updateOrCreateConfigEntities(stdConfig.getDataPrivacy(),parameters)
            putConfigEntities(stdConfig.getDataPrivacy(),parameters)

    if config["anomalyDetectionapplications"]:
        logger.info("++++++++ APPLICATION ANOMALY DETECTION ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: anomalyDetectionapplications")
        else:
            # anomalyDetection Applications
            updateOrCreateConfigEntities(stdConfig.getAnomalyDetectionApplications(),parameters)
            putConfigEntities(stdConfig.getAnomalyDetectionApplications(),parameters)

    if config["anomalyDetectionservices"]:
        logger.info("++++++++ SERVICES ANOMALY DETECTION ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: anomalyDetectionservices")
        else:
            # anomalyDetection Applications
            updateOrCreateConfigEntities(stdConfig.getAnomalyDetectionServices(),parameters)
            putConfigEntities(stdConfig.getAnomalyDetectionServices(),parameters)

    
    if config["applicationsweb"]:
        logger.info("++++++++ APPLICATIONS ++++++++")
        # applications
        getServices(parameters)
        getApplications(parameters)
        apps = createAppConfigEntitiesFromServices()
        if config["dryrun"]:
            logger.info("Dryrun: applicationsweb")
        else:
            putApplicationConfigs(apps)
        
    if config["syntheticMonitors"]:
        # synthetic monitors
        if not config["applications"]:
            getServices(parameters)
            getApplications(parameters)
            apps = createAppConfigEntitiesFromServices()
        
        logger.info("++++++++ SYNTHETIC MONITORS ++++++++")
        monitors = createSyntheticMonitorsFromApps(apps)
        if config["dryrun"]:
            logger.info("Dryrun: syntheticMonitors")
        else:
            putSyntheticMonitors(monitors)

    if config["applicationDashboards"]:
        logger.info("++++++++ APPLICATION DASHBOARDS ++++++++")
        # application dashboards
        # these dashboards are created per application dynamically
        if not config["applicationsweb"]:
            getServices(parameters)
            getApplications(parameters)
            apps = createAppConfigEntitiesFromServices()
        appdashboards = createAppDashboardEntitiesFromApps(apps)
        
        if config["dryrun"]:
            logger.info("Dryrun: applicationDashboards")
        else:
            putAppDashboards(appdashboards)

    if config["dashboards"]:
        logger.info("++++++++ DASHBOARDS ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: dashboards")
        else:
            # dashboards that do not need parameterization
            putConfigEntities(stdConfig.getDashboards(),parameters)

    if config["alertingProfiles"]:
        logger.info("++++++++ ALERTING PROFILES ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: alertingprofiles")
        else:
            #alertingprofiles  
            #purgeConfigEntities([ConfigTypes.alertingProfiles], parameters)
            updateOrCreateConfigEntities(stdConfig.getAlertingProfiles(),parameters)
            putConfigEntities(stdConfig.getAlertingProfiles(),parameters)

    if config["notifications"]:
        logger.info("++++++++ NOTIFICATIONS ++++++++")
        if config["dryrun"]:
            logger.info("Dryrun: notifications")
        else:
            #notifications  
            #purgeConfigEntities([ConfigTypes.notifications], parameters)
            updateOrCreateConfigEntities(stdConfig.getNotifications(),parameters)
            putConfigEntities(stdConfig.getNotifications(),parameters)

def getConfig(parameters):
    #configtypes = [ConfigTypes.servicerequestAttributes, ConfigTypes.customServicesjava, ConfigTypes.calculatedMetricsservice, ConfigTypes.autoTags, ConfigTypes.servicerequestNaming, ConfigTypes.notifications]
    #configtypes = [getattr(ConfigTypes,cls.__name__)(id="",name="") for cls in ConfigTypes.TenantConfigEntity.__subclasses__()][1:]
    configtypes = [getattr(ConfigTypes,cls.__name__) for cls in ConfigTypes.TenantConfigEntity.__subclasses__()][1:]
    getConfigSettings(configtypes, parameters)  

def main(argv):
    logger.info(stdConfig)

    #subscribe to a control channel, we will listen for
    cfgcontrol = configcache.pubsub()
    cfgcontrol.subscribe('configcontrol')

    #list all known config entity types we are aware of
    logger.info("Able to manage these configuration entities of tenants: {}".format([cls.__name__ for cls in ConfigTypes.TenantConfigEntity.__subclasses__()][1:]))
    logger.info("Able to manage these entities of tenants: {}".format([cls.__name__ for cls in ConfigTypes.TenantEntity.__subclasses__()]))

    while True:
        message = cfgcontrol.get_message()
        if message:
            command = message['data']
            #logger.info("Received Command: {}".format(command))
            if command == 'START_CONFIG':
                logger.info("========== STARTING CONFIG PUSH ==========")
                params = configcache.get("parameters")
                if params:
                    parameters = json.loads(params)
                    logger.info("Found Parameters: {}".format(parameters))
                    getConfig(parameters)
                    performConfig(parameters)    

                    # cleanup redis
                    allkeys = configcache.keys("*")
                    for key in allkeys:
                        if key != "config" and key != "parameters":
                            configcache.delete(key)
                    # but keep the parameters
                    #configcache.set("parameters",json.dumps(parameters))
                    configcache.publish('configcontrol','FINISHED_CONFIG')
                else:
                    logger.warning("No Parameters found in config cache ... skipping")
            
                logger.info("========== FINISHED CONFIG PUSH ==========")

            if command == 'DUMP_CONFIG':
                logger.info("========== STARTING CONFIG PULL ==========")
            
        time.sleep(5)


if __name__ == "__main__":
   main(sys.argv[1:])
    