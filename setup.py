from setuptools import setup

setup(
    name='logless',
    packages=['logless'],
    version='0.0.1',
    description='LogLess CLI for setting up LogLess lambda environment',
    author='Darko Ronic',
    author_email='darko.ronic@gmail.com',
    license='MIT',
    url='http://github.com/apolloFER/logless',
    install_requires=[
        'logless-lambda',
        'click',
        "boto3",
        "PyYAML",
        "tqdm"
    ],
    entry_points='''
    [console_scripts]
    logless=logless.cli:main

    ''',

)
