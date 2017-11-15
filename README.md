# LogLess
> LogLess is a centralized logging/event stack using AWS Kinesis/Lambda. The main CLI for creating the AWS environment.

## Usage

LogLess is just a small part of the entire system. This is the CLI used to create the entire AWS environment and to jumpstart the logs processing.

You still need other parts of the system:
 - [LogLess Python logging handler](https://github.com/apolloFER/logless-python) - used for forwarding Python logs to LogLess Kinesis stream
 - [LogLess Golang logrus hook](https://github.com/apolloFER/logless-golang) - used for forwarding Golang logrus logs to LogLess Kinesis stream
 - [LogLess Lambda handler](https://github.com/apolloFER/logless-lambda) - Kinesis messages processor for AWS Lambda, embeeded in LogLess CLI
 
LogLess CLI creates your LogLess environment.

It will create the project structure (`logless-settings.yaml` and `handler.py` files). Add your processing code to `handler.py`.

Navigate to the folder where you want to init your LogLess project. Create a Python2/3 virtualenv and activate it.

To init the LogLess project run:

`logless init`

This will create the two files mentioned beforehand. Update the `handler.py` with your code (or don't, I don't care). First deployment should be done with:

`logless deploy`

This will create all AWS services and connect them together.

After updating your code you can deploy the changes by running:

`logless update`


### License
Apache License 2.0