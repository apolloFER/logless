import time
import uuid
import json
import os
import inspect
import sys
import botocore
import boto3
import click
import random
import string
import yaml
import getpass
import tempfile
import subprocess
import glob
import stat
import shutil
import zipfile

from distutils.dir_util import copy_tree
from setuptools import find_packages
from tqdm import tqdm

BOTO3_CONFIG_DOCS_URL = 'https://boto3.readthedocs.io/en/latest/guide/quickstart.html#configuration'

HANDLER_TEMPLATE = """import logless_lambda


def processor(event, context):
    for record in logless_lambda.logless_records(event['Records']):
        \"\"\"
        Process records received from the logless_lambda library.
        \"\"\"
        print("Received record", record.message, record.timestamp, record.level, record.source)
"""

ASSUME_POLICY = """{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "",
      "Effect": "Allow",
      "Principal": {
        "Service": [
          "apigateway.amazonaws.com",
          "lambda.amazonaws.com",
          "events.amazonaws.com"
        ]
      },
      "Action": "sts:AssumeRole"
    }
  ]
}"""

ATTACH_POLICY = """{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:*"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "lambda:InvokeFunction"
            ],
            "Resource": [
                "*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:*"
            ],
            "Resource": "arn:aws:s3:::*"
        },
        {
            "Effect": "Allow",
            "Action": "kinesis:*",
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "sns:*"
            ],
            "Resource": "arn:aws:sns:*:*:*"
        }
    ]
}"""

MEMORY_SIZE = 128
TIMEOUT = 30
RUNTIME = "python3.6"


def human_size(num, suffix='B'):
    """
    Convert bytes length to a human-readable version
    """
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "{0:3.1f}{1!s}{2!s}".format(num, unit, suffix)
        num /= 1024.0
    return "{0:.1f}{1!s}{2!s}".format(num, 'Yi', suffix)


def copy_editable_packages(egg_links, temp_package_path):
    """ """
    for egg_link in egg_links:
        with open(egg_link, 'rb') as df:
            egg_path = df.read().decode('utf-8').splitlines()[0].strip()
            pkgs = set([x.split(".")[0] for x in find_packages(egg_path, exclude=['test', 'tests'])])
            for pkg in pkgs:
                copytree(os.path.join(egg_path, pkg), os.path.join(temp_package_path, pkg), symlinks=False)

    if temp_package_path:
        # now remove any egg-links as they will cause issues if they still exist
        for link in glob.glob(os.path.join(temp_package_path, "*.egg-link")):
            os.remove(link)


def copytree(src, dst, symlinks=False, ignore=None):
    """
    This is a contributed re-implementation of 'copytree' that
    should work with the exact same behavior on multiple platforms.
    """

    if not os.path.exists(dst):
        os.makedirs(dst)
        shutil.copystat(src, dst)
    lst = os.listdir(src)

    if ignore:
        excl = ignore(src, lst)
        lst = [x for x in lst if x not in excl]

    for item in lst:
        s = os.path.join(src, item)
        d = os.path.join(dst, item)

        if symlinks and os.path.islink(s): # pragma: no cover
            if os.path.lexists(d):
                os.remove(d)
            os.symlink(os.readlink(s), d)
            try:
                st = os.lstat(s)
                mode = stat.S_IMODE(st.st_mode)
                os.lchmod(d, mode)
            except:
                pass  # lchmod not available
        elif os.path.isdir(s):
            copytree(s, d, symlinks, ignore)
        else:
            shutil.copy2(s, d)


def get_venv_from_python_version():
    return 'python' + str(sys.version_info[0]) + '.' + str(sys.version_info[1])


def contains_python_files_or_subdirs(folder):
    """
    Checks (recursively) if the directory contains .py or .pyc files
    """
    for root, dirs, files in os.walk(folder):
        if [filename for filename in files if filename.endswith('.py') or filename.endswith('.pyc')]:
            return True

        for d in dirs:
            for _, subdirs, subfiles in os.walk(d):
                if [filename for filename in subfiles if filename.endswith('.py') or filename.endswith('.pyc')]:
                    return True

    return False


def conflicts_with_a_neighbouring_module(directory_path):
    """
    Checks if a directory lies in the same directory as a .py file with the same name.
    """
    parent_dir_path, current_dir_name = os.path.split(os.path.normpath(directory_path))
    neighbours = os.listdir(parent_dir_path)
    conflicting_neighbour_filename = current_dir_name+'.py'
    return conflicting_neighbour_filename in neighbours


def get_current_venv():
    """
    Returns the path to the current virtualenv
    """
    if 'VIRTUAL_ENV' in os.environ:
        venv = os.environ['VIRTUAL_ENV']
    elif os.path.exists('.python-version'):  # pragma: no cover
        try:
            subprocess.check_output('pyenv help', stderr=subprocess.STDOUT)
        except OSError:
            print("This directory seems to have pyenv's local venv, "
                  "but pyenv executable was not found.")
        with open('.python-version', 'r') as f:
            env_name = f.readline().strip()
        bin_path = subprocess.check_output(['pyenv', 'which', 'python']).decode('utf-8')
        venv = bin_path[:bin_path.rfind(env_name)] + env_name
    else:  # pragma: no cover
        return None
    return venv


def load_settings():
    settings_file = "logless_settings.yaml"
    with open(settings_file) as settings_file_yaml:
        try:
            logless_settings = yaml.load(settings_file_yaml)
        except ValueError:
            raise ValueError("Unable to load the LogLess settings YAML. It may be malformed.")
        else:
            return logless_settings


def upload_to_s3(source_path, bucket_name, aws_region, disable_progress=False):
    """
    Given a file, upload it to S3.
    Credentials should be stored in environment variables or ~/.aws/credentials (%USERPROFILE%\.aws\credentials on Windows).
    Returns True on success, false on failure.
    """

    s3_client = boto3.client("s3")

    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError:
        # This is really stupid S3 quirk. Technically, us-east-1 one has no S3,
        # it's actually "US Standard", or something.
        # More here: https://github.com/boto/boto3/issues/125
        if aws_region == 'us-east-1':
            s3_client.create_bucket(
                Bucket=bucket_name,
            )
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={'LocationConstraint': aws_region},
            )

    if not os.path.isfile(source_path) or os.stat(source_path).st_size == 0:
        print("Problem with source file {}".format(source_path))
        return False

    dest_path = os.path.split(source_path)[1]

    try:
        source_size = os.stat(source_path).st_size
        print("\nUploading {0} ({1})..".format(dest_path, human_size(source_size)))
        progress = tqdm(total=float(os.path.getsize(source_path)), unit_scale=True, unit='B',
                        disable=disable_progress)

        # Attempt to upload to S3 using the S3 meta client with the progress bar.
        # If we're unable to do that, try one more time using a session client,
        # which cannot use the progress bar.
        # Related: https://github.com/boto/boto3/issues/611
        try:
            s3_client.upload_file(
                source_path, bucket_name, dest_path,
                Callback=progress.update
            )
        except Exception as e:  # pragma: no cover
            s3_client.upload_file(source_path, bucket_name, dest_path)

        progress.close()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        raise
    except Exception as e:  # pragma: no cover
        print(e)
        return False
    return True


def create_lambda_zip(settings, handler_file, exclude):
    import pip

    build_time = str(int(time.time()))
    venv = get_current_venv()

    cwd = os.getcwd()
    archive_fname = 'lambda_logless_package' + '-' + build_time + '.zip'
    archive_path = os.path.join(cwd, archive_fname)

    exclude.append(archive_path)
    exclude.append('concurrent')

    def splitpath(path):
        parts = []
        (path, tail) = os.path.split(path)
        while path and tail:
            parts.append(tail)
            (path, tail) = os.path.split(path)
        parts.append(os.path.join(path, tail))
        return list(map(os.path.normpath, parts))[::-1]

    split_venv = splitpath(venv)
    split_cwd = splitpath(cwd)

    # Ideally this should be avoided automatically,
    # but this serves as an okay stop-gap measure.
    if split_venv[-1] == split_cwd[-1]:  # pragma: no cover
        print(
            "Warning! Your project and virtualenv have the same name! You may want "
            "to re-create your venv with a new name, or explicitly define a "
            "'project_name', as this may cause errors."
        )

    temp_project_path = os.path.join(tempfile.gettempdir(), str(int(time.time())))

    os.makedirs(temp_project_path)

    package_info = {}
    package_info['uuid'] = str(uuid.uuid4())
    package_info['build_time'] = build_time
    package_info['build_platform'] = os.sys.platform
    package_info['build_user'] = getpass.getuser()

    package_id_file = open(os.path.join(temp_project_path, 'package_info.json'), 'w')
    dumped = json.dumps(package_info, indent=4)
    try:
        package_id_file.write(dumped)
    except TypeError:  # This is a Python 2/3 issue. TODO: Make pretty!
        package_id_file.write(unicode(dumped))
    package_id_file.close()

    # Then, do site site-packages..
    egg_links = []
    temp_package_path = os.path.join(tempfile.gettempdir(), str(int(time.time() + 1)))
    if os.sys.platform == 'win32':
        site_packages = os.path.join(venv, 'Lib', 'site-packages')
    else:
        site_packages = os.path.join(venv, 'lib', get_venv_from_python_version(), 'site-packages')
    egg_links.extend(glob.glob(os.path.join(site_packages, '*.egg-link')))

    copytree(site_packages, temp_package_path, symlinks=False)

    site_packages_64 = os.path.join(venv, 'lib64', get_venv_from_python_version(), 'site-packages')
    if os.path.exists(site_packages_64):
        egg_links.extend(glob.glob(os.path.join(site_packages_64, '*.egg-link')))
        copytree(site_packages_64, temp_package_path, symlinks=False)

    if egg_links:
        copy_editable_packages(egg_links, temp_package_path)

    copy_tree(temp_package_path, temp_project_path, update=True)

    try:
        compression_method = zipfile.ZIP_DEFLATED
    except ImportError:  # pragma: no cover
        compression_method = zipfile.ZIP_STORED
    archivef = zipfile.ZipFile(archive_path, 'w', compression_method)

    for root, dirs, files in os.walk(temp_project_path):

        for filename in files:
            # If there is a .pyc file in this package,
            # we can skip the python source code as we'll just
            # use the compiled bytecode anyway..
            if filename[-3:] == '.py' and root[-10:] != 'migrations':
                abs_filname = os.path.join(root, filename)
                abs_pyc_filename = abs_filname + 'c'
                if os.path.isfile(abs_pyc_filename):

                    # but only if the pyc is older than the py,
                    # otherwise we'll deploy outdated code!
                    py_time = os.stat(abs_filname).st_mtime
                    pyc_time = os.stat(abs_pyc_filename).st_mtime

                    if pyc_time > py_time:
                        continue

            # Make sure that the files are all correctly chmodded
            os.chmod(os.path.join(root, filename), 0o755)


            # Actually put the file into the proper place in the zip
            zipi = zipfile.ZipInfo(os.path.join(root.replace(temp_project_path, '').lstrip(os.sep), filename))
            zipi.create_system = 3
            zipi.external_attr = 0o755 << int(16)  # Is this P2/P3 functional?
            with open(os.path.join(root, filename), 'rb') as f:
                archivef.writestr(zipi, f.read(), compression_method)

        # Create python init file if it does not exist
        # Only do that if there are sub folders or python files and does not conflict with a neighbouring module
        if not contains_python_files_or_subdirs(root):
            # if the directory does not contain any .py file at any level, we can skip the rest
            dirs[:] = [d for d in dirs if d != root]
        else:
            if '__init__.py' not in files and not conflicts_with_a_neighbouring_module(root):
                tmp_init = os.path.join(temp_project_path, '__init__.py')
                open(tmp_init, 'a').close()
                os.chmod(tmp_init, 0o755)

                arcname = os.path.join(root.replace(temp_project_path, ''),
                                       os.path.join(root.replace(temp_project_path, ''), '__init__.py'))
                archivef.write(tmp_init, arcname)

        # And, we're done!
        archivef.close()

        # Trash the temp directory
        shutil.rmtree(temp_project_path)
        shutil.rmtree(temp_package_path)

        return archive_fname


def create_package(settings):
    region = settings["aws_region"]
    logless_id = settings["logless_id"]

    current_file = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    handler_file = os.sep.join(current_file.split(os.sep)[0:]) + os.sep + 'handler.py'

    # Custom excludes for different versions.
    if sys.version_info[0] < 3:
        # Exclude packages already builtin to the python lambda environment
        exclude = [
            "boto3",
            "dateutil",
            "botocore",
            "s3transfer",
            "six.py",
            "jmespath",
            "concurrent"
        ]
    else:
        # This could be python3.6 optimized.
        exclude = [
            "boto3",
            "dateutil",
            "botocore",
            "s3transfer",
            "concurrent"
        ]

    zip_path = create_lambda_zip(settings, handler_file, exclude)
    success = upload_to_s3(zip_path, logless_id, region)
    if not success:  # pragma: no cover
        raise click.ClickException("Unable to upload to S3. Quitting.")

    return zip_path


def remove_local_file(zip_path):
    try:
        if os.path.isfile(zip_path):
            os.remove(zip_path)
    except Exception as e:  # pragma: no cover
        sys.exit(-1)


def remove_from_s3(file_name, bucket_name):
    """
    Given a file name and a bucket, remove it from S3.
    There's no reason to keep the file hosted on S3 once its been made into a Lambda function, so we can delete it from S3.
    Returns True on success, False on failure.
    """
    s3_client = boto3.client("s3")

    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError as e:  # pragma: no cover
        # If a client error is thrown, then check that it was a 404 error.
        # If it was a 404 error, then the bucket does not exist.
        error_code = int(e.response['Error']['Code'])
        if error_code == 404:
            return False

    try:
        s3_client.delete_object(Bucket=bucket_name, Key=file_name)
        return True
    except botocore.exceptions.ClientError:  # pragma: no cover
        return False


def delete_zip_package(settings, zip_path):
    logless_id = settings["logless_id"]

    remove_from_s3(zip_path, logless_id)
    remove_local_file(zip_path)


def get_credentials_arn(logless_id, region):
    iam = boto3.resource("iam", region_name=region)
    role = iam.Role(logless_id)
    return role, role.arn


def create_policy(settings):
    attach_policy_obj = json.loads(ATTACH_POLICY)
    assume_policy_obj = json.loads(ASSUME_POLICY)

    logless_id = settings["logless_id"]
    region = settings["aws_region"]
    iam = boto3.resource("iam", region_name=region)
    iam_client = boto3.client("iam", region_name=region)

    updated = False

    # Create the role if needed
    try:
        role, credentials_arn = get_credentials_arn(logless_id, region)
    except botocore.client.ClientError:
        print("Creating " + logless_id + " IAM Role..")

        role = iam.create_role(
            RoleName=logless_id,
            AssumeRolePolicyDocument=ASSUME_POLICY
        )
        credentials_arn = role.arn
        updated = True

    # create or update the role's policies if needed
    policy = iam.RolePolicy(logless_id, 'logless-permissions')
    try:
        if policy.policy_document != attach_policy_obj:
            print("Updating logless-permissions policy on " + logless_id + " IAM Role.")

            policy.put(PolicyDocument=ATTACH_POLICY)
            updated = True

    except botocore.client.ClientError:
        print("Creating logless-permissions policy on " + logless_id + " IAM Role.")
        policy.put(PolicyDocument=ATTACH_POLICY)
        updated = True

    if role.assume_role_policy_document != assume_policy_obj and \
                    set(role.assume_role_policy_document['Statement'][0]['Principal']['Service']) != set(
                assume_policy_obj['Statement'][0]['Principal']['Service']):
        print("Updating assume role policy on " + logless_id + " IAM Role.")
        iam_client.update_assume_role_policy(
            RoleName=logless_id,
            PolicyDocument=ASSUME_POLICY
        )
        updated = True

    return credentials_arn


def create_kinesis(settings):
    region = settings["aws_region"]
    logless_id = settings["logless_id"]
    shard_count = settings["kinesis_shards"]
    kinesis_client = boto3.client("kinesis", region_name=region)

    click.echo("\nCreating Kinesis stream " + click.style(logless_id, bold=True))

    kinesis_client.create_stream(StreamName=logless_id, ShardCount=shard_count)

    while True:
        click.echo("Waiting for stream " + click.style(logless_id, bold=True) + " to become active.")
        kinesis_stream = kinesis_client.describe_stream(StreamName=logless_id)
        if kinesis_stream["StreamDescription"]["StreamStatus"] == 'ACTIVE':
            kinesis_arn = kinesis_stream["StreamDescription"]["StreamARN"]
            break
        time.sleep(5)

    return kinesis_arn


def create_lambda(settings, zip_path):
    region = settings["aws_region"]
    logless_id = settings["logless_id"]
    handler = settings["handler"]

    lambda_client = boto3.client("lambda", region_name=region)

    role, role_arn = get_credentials_arn(logless_id, region)

    code = {'S3Bucket': logless_id,
            'S3Key': zip_path}

    click.echo("\nCreating Lambda function " + click.style(logless_id, bold=True) + "!\n")

    lambda_function = lambda_client.create_function(FunctionName=logless_id,
                                                    Role=role_arn,
                                                    MemorySize=MEMORY_SIZE,
                                                    Timeout=TIMEOUT,
                                                    Runtime=RUNTIME,
                                                    Handler=handler,
                                                    Code=code)
    lambda_arn = lambda_function['FunctionArn']

    click.echo("Lambda function " + click.style(logless_id, bold=True) + " created, arn: " + lambda_arn)

    return lambda_arn


def update_lambda(settings, zip_path):
    region = settings["aws_region"]
    logless_id = settings["logless_id"]

    lambda_client = boto3.client("lambda", region_name=region)

    lambda_client.update_function_code(FunctionName=logless_id, Publish=True, S3Bucket=logless_id, S3Key=zip_path)


def create_event_trigger(settings, kinesis_arn):
    region = settings["aws_region"]
    logless_id = settings["logless_id"]

    lambda_client = boto3.client("lambda", region_name=region)

    click.echo("\nConnecting Kinesis and Lambda!")

    lambda_client.create_event_source_mapping(EventSourceArn=kinesis_arn,
                                              FunctionName=logless_id,
                                              Enabled=True,
                                              StartingPosition='TRIM_HORIZON')


@click.group()
def cli():
    pass


@cli.command()
def init():
    click.echo(click.style(u"""\n██╗      ██████╗  ██████╗ ██╗     ███████╗███████╗███████╗
██║     ██╔═══██╗██╔════╝ ██║     ██╔════╝██╔════╝██╔════╝
██║     ██║   ██║██║  ███╗██║     █████╗  ███████╗███████╗
██║     ██║   ██║██║   ██║██║     ██╔══╝  ╚════██║╚════██║
███████╗╚██████╔╝╚██████╔╝███████╗███████╗███████║███████║
╚══════╝ ╚═════╝  ╚═════╝ ╚══════╝╚══════╝╚══════╝╚══════╝\n""", fg='yellow', bold=True))

    click.echo(
        click.style("Welcome to ", bold=True) + click.style("LogLess", fg='yellow', bold=True) + click.style("!\n",
                                                                                                             bold=True))
    click.echo(click.style("LogLess", bold=True) + " is a framework for centralized log processing using AWS Kinesis"
                                                   " and AWS Lambda.")
    click.echo("This `init` command will help you create and configure your new LogLess deployment.")
    click.echo("Let's get started!\n")

    session = botocore.session.Session()
    config = session.full_config
    profiles = config.get("profiles", {})
    profile_names = list(profiles.keys())

    click.echo("\nAWS Lambda is only available in certain regions. "
               "Let's check to make sure you have a profile set up in one that will work.")

    if not profile_names:
        profile_name, profile = None, None
        click.echo(
            "We couldn't find an AWS profile to use. Before using LogLess, you'll need to set one up. See here for more info: {}"
                .format(click.style(BOTO3_CONFIG_DOCS_URL, fg="blue", underline=True)))
    elif len(profile_names) == 1:
        profile_name = profile_names[0]
        profile = profiles[profile_name]
        click.echo("Okay, using profile {}!".format(click.style(profile_name, bold=True)))
    else:
        if "default" in profile_names:
            default_profile = [p for p in profile_names if p == "default"][0]
        else:
            default_profile = profile_names[0]

        while True:
            profile_name = click.prompt(
                "We found the following profiles: {}, and {}. Which would you like us to use? (default '{}')".format(
                    ', '.join(profile_names[:-1]),
                    profile_names[-1],
                    default_profile)) or default_profile
            if profile_name in profiles:
                profile = profiles[profile_name]
                break
            else:
                click.echo("Please enter a valid name for your AWS profile.")

    profile_region = profile.get("region") if profile else None
    logless_id = "logless-" + ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(9))

    # Create Bucket
    click.echo(
        "\nYour LogLess Lambda will need to be uploaded to a " + click.style("private S3 bucket", bold=True) + ".")
    click.echo("A new S3 bucket with the name "
               + click.style(logless_id, bold=True)
               + " has will be created. "
                 "This will be the name of your LogLess deployment.")

    # Select number of Shards
    click.echo("\nLogLess uses AWS Kinesis for log/event streaming. Kinesis requires a preset number of shards.")
    shard_count = click.prompt("Enter the number of shards for your Kinesis stream", type=int)

    logless_settings = {
        "logless_id": logless_id,
        "aws_region": profile_region,
        "kinesis_shards": shard_count,
        "runtime": 'python3.6' if sys.version_info[0] == 3 else 'python2.7',
        "handler": "handler.process"
    }

    logless_settings_yaml = yaml.dump(logless_settings, default_flow_style=False)

    click.echo("\nOkay, here's your " + click.style("logless_settings.yaml", bold=True) + ":\n")
    click.echo(click.style(logless_settings_yaml, fg="yellow", bold=False))

    confirm = click.prompt("Does this look " + click.style("okay", bold=True, fg="green") + "? (default 'y') [y/n]: ",
                           default='y')
    if confirm[0] not in ['y', 'Y', 'yes', 'YES']:
        click.echo("" + click.style("Sorry", bold=True, fg='red') + " to hear that! Please init again.")
        return

    with open("logless_settings.yaml", "w") as logless_yaml_file:
        logless_yaml_file.write(logless_settings_yaml)

    with open("handler.py", "w") as handler_file:
        handler_file.write(HANDLER_TEMPLATE)

    click.echo("\nGenerating handler code!")

    click.echo("\nYou can find the handler template code in the "
               + click.style("handler.py", bold=True)
               + " file. Put your procesing code there.")

    click.echo("\n" + click.style("Done", bold=True)
               + "! Now you can "
               + click.style("deploy",
                             bold=True)
               + " your LogLess environment by executing:\n")
    click.echo(click.style("\t$ logless deploy", bold=True))

    click.echo("\nAfter that, you can " + click.style("update", bold=True) + " your application code with:\n")
    click.echo(click.style("\t$ logless update\n", bold=True))


@cli.command()
def deploy():
    logless_settings = load_settings()
    logless_id = logless_settings["logless_id"]

    create_policy(logless_settings)

    zip_file = create_package(logless_settings)

    kinesis_arn = create_kinesis(logless_settings)

    create_lambda(logless_settings, zip_file)

    create_event_trigger(logless_settings, kinesis_arn)

    delete_zip_package(logless_settings, zip_file)

    click.echo("\nLogLess sucessfully deployed. "
               + "You can start generating logs from Python and Golang using the logless libraries.")
    click.echo("Use " + click.style(logless_id, bold=True) + " as Kinesis stream in your application.")

    click.echo("\nAfter changing your handler code, you can " + click.style("update", bold=True) + " it with:\n")
    click.echo(click.style("\t$ logless update\n", bold=True))


@cli.command()
def update():
    logless_settings = load_settings()

    zip_file = create_package(logless_settings)

    update_lambda(logless_settings, zip_file)

    delete_zip_package(logless_settings, zip_file)


def main():
    cli(obj={}, auto_envvar_prefix="LOGLESS")
