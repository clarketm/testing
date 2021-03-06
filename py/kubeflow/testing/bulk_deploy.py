"""Launch a bunch of K8s jobs to deploy Kubeflow.

This is primarily intended to setup Kubeflow instances in codelab projects.
"""

import datetime
import logging
import os
import tempfile
import fire
import uuid
import yaml

from google.cloud import storage
from kubeflow.testing import util
from kubernetes import config
from kubernetes import client as k8s_client

# Currently in the kubeflow/kubeflow repo
from testing import gcp_util

# The Google service account to grant access to the deployed Kubeflow instances
ADMIN_ACCOUNT = "codelab-admin-user@kf-codelab-admin.iam.gserviceaccount.com"

DEFAULT_NAMESPACE = "kubeflow-jlewi"

DEFAULT_OAUTH_FILE = "gs://kf-codelab-admin/test-project-iap.oauth.yaml"

# The format for user accounts
USER_TEMPLATE = "devstar{0}@gcplab.me"

class BulkDeploy:
  def _load_oauth_file(self, oauth_file, admin_project):
    bucket, blob_path = util.split_gcs_uri(oauth_file)

    client = storage.Client(project=admin_project)
    bucket = client.get_bucket(bucket)

    blob = bucket.get_blob(blob_path)
    contents = blob.download_as_string()

    return yaml.load(contents)

  def _default_job_file(self):
    this_dir = os.path.dirname(__file__)
    job_file = os.path.join(this_dir, "..", "..", "..", "codelabs",
                            "setup-codelab-project.yaml")
    job_file = os.path.abspath(job_file)
    return job_file

  def _create_job_spec(self, job_file, group_label, project, user, namespace):
    """Create the K8s job spec to deploy Kubeflow.

    Args:
      job_file: YAML file containing the job to use as a template
      group_label: Label for the group of jobs
      project: GCP project to deploy into
      user: User to grant access to Kubeflow
      namespace: Namespace where job should run.
    """
    # Load a new copy of the job template on each run
    with open(job_file) as hf:
      job = yaml.load(hf)

    user_label = user.replace("@", "AT")
    labels = {
      "project": project,
      "user": user_label,
      "group": group_label,
    }

    job["metadata"]["namespace"] = namespace
    job["metadata"]["labels"].update(labels)
    job["spec"]["template"]["metadata"]["labels"].update(labels)

    # Process the command line
    remove_args = ["--project", "--extra_users", "--email"]
    command = []
    for a in job["spec"]["template"]["spec"]["containers"][0]["command"]:
      keep = True
      for r in remove_args:
        if a.startswith(r):
          keep = False
          break

      if not keep:
        continue

      command.append(a)

    command.append("--email=" + user)
    extra_users = ["serviceAccount:" + ADMIN_ACCOUNT]
    command.append("--project=" + project)
    command.append("--extra_users=" + ",".join(extra_users))

    job["spec"]["template"]["spec"]["containers"][0]["command"] = command


    logging.info("Job spec for project: %s\n%s", project,
                 yaml.safe_dump(job))

    return job

  def _create_delete_job_spec(self, job_file, group_label, project, kfname,
                              namespace):
    """Create the K8s job spec to delete a kubeflow deployment.

    Args:
      job_file: YAML file containing the job to use as a template
      group_label: Label for the group of jobs
      project: GCP project to deploy into
      kfname: Name of the kubeflow deployment
      namespace: Namespace where job should run.
    """
    # Load a new copy of the job template on each run
    with open(job_file) as hf:
      job = yaml.load(hf)

    labels = {
      "project": project,
      "group": group_label,
    }

    job["metadata"]["generateName"] = "delete-codelab-"
    job["metadata"]["namespace"] = namespace
    job["metadata"]["labels"].update(labels)
    job["spec"]["template"]["metadata"]["labels"].update(labels)

    job["spec"]["template"]["spec"]["containers"][0]["command"] = [
      "python",
      "-m",
      "kubeflow.testing.delete_kf_instance",
      "delete_kf",
      "--project=" + project,
      "--name=" + kfname,
    ]

    logging.info("Job spec for project: %s\n%s", project,
                 yaml.safe_dump(job))

    return job

  def deploy(self, project_base_name, start_index, end_index,  # pylint: disable=too-many-arguments
             job_file=None, output_dir=None, namespace=DEFAULT_NAMESPACE):
    """Fire off a bunch of K8s jobs to deploy many Kubeflow instances.

    Args:
      project_base_name: The base name for the projects. Should end
        with "-"
      start_index: The start index
      end_index: The index non inclusive
      kfname: The name of the of the kubeflow app

      job_file: The path to the YAML file containing a K8s Job that serves
        as the template for the jobs to be launched.

      output_dir: Directory to write the job specs to.
    """
    util.load_kube_credentials()

    # Create an API client object to talk to the K8s master.
    api_client = k8s_client.ApiClient()
    batch_api = k8s_client.BatchV1Api(api_client)

    if not job_file:
      job_file = self._default_job_file()

    if not os.path.exists(job_file):
      raise ValueError("job file {0} does not exist".format(job_file))

    logging.info("Job file: %s", job_file)

    if not output_dir:
      output_dir = tempfile.mkdtemp()

    logging.info("output_dir: %s", output_dir)

    # Generate a common label for all the jobs. This way we can potentially
    # wait for all the jobs based on the label.
    group_label = (datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") +
                   uuid.uuid4().hex[0:4])
    for index in range(start_index, end_index):
      project = "{0}{1}".format(project_base_name, index)
      user = USER_TEMPLATE.format(index)
      logging.info("Processing project=%s user=%s", project, user)

      logging.info("Processing project=%s user=%s", project, user)

      job = self._create_job_spec(job_file, group_label, project, user,
                                  namespace)

      output_file = os.path.join(output_dir, "setup-{0}.yaml".format(project))
      logging.info("Writing job spec to %s", output_file)
      with open(output_file, "w") as hf:
        yaml.safe_dump(job, hf)

      # submit the job
      logging.info("Creating job")
      actual_job = batch_api.create_namespaced_job(job["metadata"]["namespace"],
                                                   job)

      logging.info("Created job %s.%s:\n%s", actual_job.metadata.namespace,
                   actual_job.metadata.name, yaml.safe_dump(actual_job.to_dict()))

    self.wait_for_jobs(namespace, "group=" + group_label)

  def delete(self, project_base_name, start_index, end_index, kfname, # pylint: disable=too-many-arguments
             job_file=None, output_dir=None, namespace=DEFAULT_NAMESPACE):
    """Fire off a bunch of K8s jobs to delete many Kubeflow instances.

    Args:
      project_base_name: The base name for the projects. Should end
        with "-"
      start_index: The start index
      end_index: The index non inclusive
      kfname: The name of the of the kubeflow app
      job_file: The path to the YAML file containing a K8s Job that serves
        as the template for the jobs to be launched.

      output_dir: Directory to write the job specs to.
    """
    util.load_kube_credentials()

    # Create an API client object to talk to the K8s master.
    api_client = k8s_client.ApiClient()
    batch_api = k8s_client.BatchV1Api(api_client)

    if not job_file:
      job_file = self._default_job_file()

    if not os.path.exists(job_file):
      raise ValueError("job file {0} does not exist".format(job_file))

    logging.info("Job file: %s", job_file)

    if not output_dir:
      output_dir = tempfile.mkdtemp()

    logging.info("output_dir: %s", output_dir)

    # Generate a common label for all the jobs. This way we can potentially
    # wait for all the jobs based on the label.
    group_label = (datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") +
                   uuid.uuid4().hex[0:4])
    for index in range(start_index, end_index):
      project = "{0}{1}".format(project_base_name, index)

      logging.info("Processing project=%s", project)

      job = self._create_delete_job_spec(job_file, group_label, project,
                                         kfname, namespace)
      output_file = os.path.join(output_dir, "delete-{0}.yaml".format(project))
      logging.info("Writing job spec to %s", output_file)
      with open(output_file, "w") as hf:
        yaml.safe_dump(job, hf)

      # submit the job
      logging.info("Creating job")
      actual_job = batch_api.create_namespaced_job(job["metadata"]["namespace"],
                                                   job)

      logging.info("Created job %s.%s:\n%s", actual_job.metadata.namespace,
                   actual_job.metadata.name, yaml.safe_dump(actual_job.to_dict()))

    self.wait_for_jobs(namespace, "group=" + group_label)

  def wait_for_jobs(self, namespace, label_filter):
    """Wait for all the jobs with the specified label to finish.

    Args:
      label_filter: A label filter expression e.g. "group=mygroup"
    """
    if not util.is_in_cluster():
      util.load_kube_config(persist_config=False)
    else:
      config.load_incluster_config()

    # Create an API client object to talk to the K8s master.
    api_client = k8s_client.ApiClient()
    jobs = util.wait_for_jobs_with_label(api_client, namespace, label_filter)

    done = 0
    succeeded = 0
    for job in jobs.items:
      project = job.metadata.labels.get("project", "")
      if not job.status.conditions:
        logging.info("Project %s Job %s.%s missing condition",
                     project, job.metadata.namespace, job.metadata.name)
        continue

      last_condition = job.status.conditions[-1]
      if last_condition.type in ["Failed", "Complete"]:
        logging.info("Project %s Job %s.%s has condition %s",
                     project, job.metadata.namespace,
                     job.metadata.name, last_condition.type)
        done += 1
        if last_condition.type in ["Complete"]:
          succeeded += 1

    logging.info("%s of %s jobs finished", done, len(jobs.items))
    logging.info("%s of %s jobs finished successfully", succeeded,
                 len(jobs.items))

  def check_endpoints(self, project_base_name, start_index, end_index,
                      kfname, admin_project="kf-codelab-admin",
                      oauth_file=DEFAULT_OAUTH_FILE):
    """Check whether the project endpoints are accessible.

    Args:
      project_base_name: The base name for the projects. Should end
        with "-"
      start_index: The start index
      end_index: The index non inclusive
      kfname: The name of the of the kubeflow app
    """
    if not kfname:
      raise ValueError("kfname is required")

    logging.info("Using Kubeflow app name: %s", kfname)

    logging.info("Reading OAuth file: %s", oauth_file)
    oauth_info = self._load_oauth_file(oauth_file, admin_project)

    # gcp_util.iap_is_ready uses magic to get client id
    os.environ["CLIENT_ID"] = oauth_info["CLIENT_ID"]
    os.environ["CLIENT_SECRET"] = oauth_info["CLIENT_SECRET"]

    not_ready = []
    ready = []
    for index in range(start_index, end_index):
      project = "{0}{1}".format(project_base_name, index)

      logging.info("Processing project=%s", project)

      url = "https://{0}.endpoints.{1}.cloud.goog/".format(kfname, project)

      # We set a very short time because we don't want to wait very long for
      # each one.
      logging.info("Checking Project %s, URL: %s", project, url)
      is_ready = gcp_util.iap_is_ready(url, wait_min=.25)

      logging.info("Project %s, URL %s, is_ready: %s", project, url, is_ready)

      if is_ready:
        ready.append(project)
      else:
        not_ready.append(project)

    logging.info("Projects that aren't ready:\n%s", "\n".join(not_ready))
    logging.info("Projects that are ready:\n%s", "\n".join(ready))

if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO,
                        format=('%(levelname)s|%(asctime)s'
                              '|%(pathname)s|%(lineno)d| %(message)s'),
                      datefmt='%Y-%m-%dT%H:%M:%S',
                            )
  logging.getLogger().setLevel(logging.INFO)
  fire.Fire(BulkDeploy)
