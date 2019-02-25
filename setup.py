from setuptools import setup

setup(
    name='dead',
    description='dead simple python dead code detection',
    url='https://github.com/asottile/dead',
    version='0.0.5',
    author='Anthony Sottile',
    author_email='asottile@umich.edu',
    classifiers=[
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
    ],
    install_requires=['identify'],
    py_modules=['dead'],
    entry_points={'console_scripts': ['dead=dead:main']},
)
