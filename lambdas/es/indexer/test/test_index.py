"""
Tests for the ES indexer. This function consumes events from SQS.
"""
from copy import deepcopy

from gzip import compress
from io import BytesIO
import json
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import ANY, patch
from urllib.parse import unquote_plus

import boto3
from botocore import UNSIGNED
from botocore.client import Config
from botocore.stub import Stubber
import pytest
import responses

from .. import index

BASE_DIR = Path(__file__).parent / 'data'

# See the following AWS docs for event structure:
EVENT_CORE = {
    "awsRegion": "us-east-1",
    "eventName": "ObjectCreated:Put",
    "eventSource": "aws:s3",
    "eventTime": "2020-05-22T00:32:20.515Z",
    "eventVersion": "2.1",
    "requestParameters": {"sourceIPAddress": "127.0.0.1"},
    "responseElements": {
        "x-amz-id-2": "EXAMPLE123/5678abcdefghijklambdaisawesome/mnopqrstuvwxyzABCDEFGH",
        "x-amz-request-id": "EXAMPLE123456789"
    },
    "s3": {
        "bucket": {
            "arn": "arn:aws:s3:::test-bucket",
            "name": "test-bucket",
            "ownerIdentity": {
                "principalId": "EXAMPLE"
            }
        },
        "configurationId": "testConfigRule",
        "object": {
            "key": "hello+world.txt",
            "sequencer": "0A1B2C3D4E5F678901"
        },
        "s3SchemaVersion": "1.0"
    },
    "userIdentity": {"principalId": "EXAMPLE"}
}

def check_event(synthetic, organic):
    # Ensure that synthetic events have the same shape as actual organic ones,
    # and that overridden properties like bucket, key, eTag are properly set
    # same keys at top level
    assert organic.keys() == synthetic.keys()
    # same value types (values might differ and that's OK)
    assert {type(v) for v in organic.values()} == \
        {type(v) for v in synthetic.values()}
    # same keys and nested under "s3"
    assert organic["s3"].keys() == synthetic["s3"].keys()
    # same value types under S3 (values might differ and that's OK)
    assert {type(v) for v in organic["s3"].values()} == \
        {type(v) for v in synthetic["s3"].values()}
    # spot checks for overridden properties
    assert organic["awsRegion"] == synthetic["awsRegion"]
    assert organic["s3"]["bucket"]["name"] == synthetic["s3"]["bucket"]["name"]
    assert organic["s3"]["bucket"]["arn"] == synthetic["s3"]["bucket"]["arn"]
    assert organic["s3"]["object"]["key"] == synthetic["s3"]["object"]["key"]
    assert organic["s3"]["object"]["eTag"] == synthetic["s3"]["object"]["eTag"]
    assert organic["s3"]["object"]["size"] == synthetic["s3"]["object"]["size"]
    assert organic["s3"]["object"]["versionId"] == synthetic["s3"]["object"]["versionId"]

def make_event(
        name,
        bucket="test-bucket",
        eTag="123456",
        key="hello+world.txt",
        region="us-east-1",
        size=100,
        versionId="1313131313131.Vier50HdNbi7ZirO65"
):
    """this function builds event types off of EVENT_CORE and adds fields
    to match organic AWS events"""
    if name in {
        "ObjectCreated:Put",
        "ObjectCreated:Copy",
        "ObjectCreated:Post",
        "ObjectCreated:CompleteMultipartUpload"
    }:
        return _make_event(
            name,
            bucket=bucket,
            eTag=eTag,
            key=key,
            region=region,
            size=size,
            versionId=versionId
        )
    # no versionId or eTag in this case
    elif name == "ObjectRemoved:Delete":
        return _make_event(
            name,
            bucket=bucket,
            key=key,
            region=region
        )
    elif name == "ObjectRemoved:DeleteMarkerCreated":
        return _make_event(
            name,
            bucket=bucket,
            eTag=eTag,
            key=key,
            region=region,
            versionId=versionId
        )
    else:
        raise ValueError(f"Unexpected event type: {name}")


def _make_event(
        name,
        bucket="",
        eTag="",
        key="",
        region="",
        size=0,
        versionId=""
):
    """make events in the pattern of
    https://docs.aws.amazon.com/AmazonS3/latest/dev/notification-content-structure.html
    and
    AWS Lambda > Console > Test Event
    """
    e = deepcopy(EVENT_CORE)
    e["eventName"] = name

    if bucket:
        e["s3"]["bucket"]["name"] = bucket
        e["s3"]["bucket"]["arn"] = f"arn:aws:s3:::{bucket}"
    if key:
        e["s3"]["object"]["key"] = key
    if eTag:
        e["s3"]["object"]["eTag"] = eTag
    if size:
        e["s3"]["object"]["size"] = size
    if region:
        e["awsRegion"] = region
    if versionId:
        e["s3"]["object"]["versionId"] = versionId

    return e


class MockContext():
    def get_remaining_time_in_millis(self):
        return 30000


class TestIndex(TestCase):
    def setUp(self):
        self.requests_mock = responses.RequestsMock(assert_all_requests_are_fired=False)
        self.requests_mock.start()

        # Create a dummy S3 client that (hopefully) can't do anything.
        self.s3_client = boto3.client('s3', config=Config(signature_version=UNSIGNED))

        self.s3_client_patcher = patch(
            __name__ + '.index.make_s3_client',
            return_value=self.s3_client
        )
        self.s3_client_patcher.start()

        self.s3_stubber = Stubber(self.s3_client)
        self.s3_stubber.activate()

        self.env_patcher = patch.dict(os.environ, {
            'ES_HOST': 'example.com',
            'AWS_ACCESS_KEY_ID': 'test_key',
            'AWS_SECRET_ACCESS_KEY': 'test_secret',
            'AWS_DEFAULT_REGION': 'ng-north-1',
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

        self.s3_stubber.assert_no_pending_responses()
        self.s3_stubber.deactivate()
        self.s3_client_patcher.stop()

        self.requests_mock.stop()

    def _get_contents(self, name, ext):
        return index.get_contents(
            'test-bucket', name, ext,
            etag='etag', version_id=None, s3_client=self.s3_client, size=123,
        )

    def test_synthetic_copy_event(self):
        """check synthetic ObjectCreated:Copy event vs organic obtained on 26-May-2020"""
        synthetic = make_event(
            "ObjectCreated:Copy",
            bucket="somebucket",
            key="events/copy-one/0.png",
            size=73499,
            eTag="7b4b71116bb21d3ea7138dfe7aabf036",
            region="us-west-1",
            versionId="Yj1vyLWcE9FTFIIrsgk.yAX7NbJrAW7g"
        )
        # actual event from S3 with a few obfuscations to protect the innocent
        organic = {
            "eventVersion": "2.1",
            "eventSource": "aws:s3",
            "awsRegion": "us-west-1",
            "eventTime": "2020-05-26T22:15:10.906Z",
            "eventName": "ObjectCreated:Copy",
            "userIdentity": {
                "principalId": "AWS:EXAMPLEDUDE"
            },
            "requestParameters": {
                "sourceIPAddress": "07.571.22.131"
            },
            "responseElements": {
                "x-amz-request-id": "CEF0E4FD6D0944D7",
                "x-amz-id-2": "EXAMPLE/+63rMdcLBMWNcsgKSIvm5wESswLYR2Vw32z4Zg4fUo8qkP4dZJoBH9m0gvhZ9/m/HAApWP+3arsz0QPph7OBVdl1"
            },
            "s3": {
                "s3SchemaVersion": "1.0",
                "configurationId": "YmJkYWUyYmYtNzg5OC00NGRiLTk0NmItNDMxNzA4NzhiZDNk",
                "bucket": {
                    "name": "somebucket",
                    "ownerIdentity": {
                        "principalId": "SAMPLE"
                    },
                    "arn": "arn:aws:s3:::somebucket"
                },
                "object": {
                    "key": "events/copy-one/0.png",
                    "size": 73499,
                    "eTag": "7b4b71116bb21d3ea7138dfe7aabf036",
                    "versionId": "Yj1vyLWcE9FTFIIrsgk.yAX7NbJrAW7g",
                    "sequencer": "005ECD94EFA9B09DD8"
                }
            }
        }
        check_event(synthetic, organic)

    def test_synthetic_put_event(self):
        """check synthetic ObjectCreated:Put event vs organic obtained on 27-May-2020"""
        synthetic = make_event(
            "ObjectCreated:Copy",
            bucket="anybucket",
            key="events/put-one/storms.parquet",
            size=923078,
            eTag="502f21cfc143fb0c35f563eda5699fa9",
            region="us-west-1",
            versionId="yYSoQSg3.BfosdUxnRSv9vFg.WAPMmfn"
        )
        # actual event from S3 with a few obfuscations to protect the innocent
        organic = {
            "eventVersion": "2.1",
            "eventSource": "aws:s3",
            "awsRegion": "us-west-1",
            "eventTime": "2020-05-27T18:57:36.268Z",
            "eventName": "ObjectCreated:Put",
            "userIdentity": {"principalId": "AWS:notgonnabehereanyway"},
            "requestParameters": {"sourceIPAddress": "12.345.67.890"},
            "responseElements": {"x-amz-request-id": "371A83BCE4341D7D",
            "x-amz-id-2": "a+example+morestuff+01343413434234234234"},
            "s3": {
                "s3SchemaVersion": "1.0",
                "configurationId": "YmJkYWUyYmYtNzg5OC00NGRiLTk0NmItNDMxNzA4NzhiZDNk",
                "bucket": {
                    "name": "anybucket",
                    "ownerIdentity": {"principalId": "myidhere"},
                    "arn": "arn:aws:s3:::anybucket"
                },
                "object": {
                    "key": "events/put-one/storms.parquet",
                    "size": 923078,
                    "eTag": "502f21cfc143fb0c35f563eda5699fa9",
                    "versionId": "yYSoQSg3.BfosdUxnRSv9vFg.WAPMmfn",
                    "sequencer": "005ECEB81C34962CFC"
                }
            }
        }
        check_event(synthetic, organic)

    def test_infer_extensions(self):
        """ensure we are guessing file types well"""
        # parquet
        assert index.infer_extensions("s3/some/file.c000", ".c000") == ".parquet", \
            "Expected .c0000 to infer as .parquet"
        # parquet, nonzero part number
        assert index.infer_extensions("s3/some/file.c001", ".c001") == ".parquet", \
            "Expected .c0001 to infer as .parquet"
        # -c0001 file
        assert index.infer_extensions("s3/some/file-c0001", "") == ".parquet", \
            "Expected -c0001 to infer as .parquet"
        # -c00111 file (should never happen)
        assert index.infer_extensions("s3/some/file-c000121", "") == "", \
            "Expected -c000121 not to infer as .parquet"
        # .txt file, should be unchanged
        assert index.infer_extensions("s3/some/file-c0000.txt", ".txt") == ".txt", \
            "Expected .txt to infer as .txt"

    def test_delete_event(self):
        """
        Check that the indexer doesn't blow up on delete events.
        """
        # don't mock head or get; they should never be called for deleted objects
        self._test_index_event(
            "ObjectRemoved:Delete",
            mock_head=False,
            mock_object=False
        )

    def test_delete_marker_event(self):
        """
        Common event in versioned; buckets, should no-op
        """
        # don't mock head or get; this event should never call them
        self._test_index_event(
            "ObjectRemoved:DeleteMarkerCreated",
            # we should never call Elastic in this case
            mock_elastic=False,
            mock_head=False,
            mock_object=False
        )

    def test_test_event(self):
        """
        Check that the indexer does not barf when it gets an S3 test notification.
        """
        event = {
            "Records": [{
                "body": json.dumps({
                    "Message": json.dumps({
                        "Service": "Amazon S3",
                        "Event": "s3:TestEvent",
                        "Time": "2014-10-13T15:57:02.089Z",
                        "Bucket": "test-bucket",
                        "RequestId": "5582815E1AEA5ADF",
                        "HostId": "8cLeGAmw098X5cv4Zkwcmo8vvZa3eH3eKxsPzbB9wrR+YstdA6Knx4Ip8EXAMPLE"
                    })
                })
            }]
        }

        index.handler(event, None)

    def test_index_file(self):
        """test indexing a single file"""
        # test all known created events
        # https://docs.aws.amazon.com/AmazonS3/latest/dev/NotificationHowTo.html
        self._test_index_event("ObjectCreated:Put")
        self._test_index_event("ObjectCreated:Copy")
        self._test_index_event("ObjectCreated:Post")
        self._test_index_event("ObjectCreated:CompleteMultipartUpload")

    @patch(__name__ + '.index.get_contents')
    def test_index_exception(self, get_mock):
        """test indexing a single file that throws an exception"""
        class ContentException(Exception):
            pass
        get_mock.side_effect = ContentException("Unable to get contents")
        with pytest.raises(ContentException):
            # get_mock already mocks get_object, so don't mock it in _test_index_event
            self._test_index_event("ObjectCreated:Put", mock_object=False)

    def _test_index_event(
            self,
            event_name,
            mock_elastic=True,
            mock_head=True,
            mock_object=True
    ):
        """
        Reusable helper function to test indexing a single text file.
        """
        event = make_event(event_name)
        records = {
            "Records": [{
                "body": json.dumps({
                    "Message": json.dumps({
                        "Records": [event]
                    })
                })
            }]
        }

        now = index.now_like_boto3()

        metadata = {
            'helium': json.dumps({
                'comment': 'blah',
                'user_meta': {
                    'foo': 'bar'
                },
                'x': 'y'
            })
        }
        # the handler unquotes keys (see index.py) so that's what we should
        # expect back from ES, hence unkey
        unkey = unquote_plus(event["s3"]["object"]["key"])
        eTag = event["s3"]["object"].get("eTag", None)
        versionId = event["s3"]["object"].get("versionId", None)
        expected_params = {
            'Bucket': event["s3"]["bucket"]["name"],
            'Key': unkey,
        }
        # We only get versionId for certain events and if bucket versioning is
        # (or was at one time?) on
        if versionId:
            expected_params["VersionId"] = versionId
        elif eTag:
            expected_params["IfMatch"] = eTag

        if mock_head:
            self.s3_stubber.add_response(
                method='head_object',
                service_response={
                    'Metadata': metadata,
                    'ContentLength': event["s3"]["object"]["size"],
                    'LastModified': now,
                },
                expected_params=expected_params
            )

        if mock_object:
            self.s3_stubber.add_response(
                method='get_object',
                service_response={
                    'Metadata': metadata,
                    'ContentLength': event["s3"]["object"]["size"],
                    'LastModified': now,
                    'Body': BytesIO(b'Hello World!'),
                },
                expected_params={
                    **expected_params,
                    'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
                }
            )

        def es_callback(request):
            response_key = 'delete' if event_name.startswith(index.EVENT_PREFIX["Removed"]) else 'index'
            actions = [json.loads(line) for line in request.body.splitlines()]
            expected = [
                {
                    response_key: {
                        '_index':  event["s3"]["bucket"]["name"],
                        '_type': '_doc',
                        '_id': f'{unkey}:{versionId}'
                    }
                },
                {
                    'comment': 'blah',
                    'content': '' if not mock_object else 'Hello World!',
                    'event': event_name,
                    'ext': os.path.splitext(unkey)[1],
                    'key': unkey,
                    'last_modified': now.isoformat(),
                    'meta_text': 'blah  {"x": "y"} {"foo": "bar"}',
                    'target': '',
                    'updated': ANY,
                }
            ]
            # conditionally define fields not present in all events
            if event["s3"]["object"].get("eTag", None):
                expected[1]["etag"] = event["s3"]["object"]["eTag"]
            if event["s3"]["object"].get("size", None):
                expected[1]["size"] = event["s3"]["object"]["size"]
            if versionId:
                expected[1]["version_id"] = versionId

            if response_key == 'delete':
                # delete events do not include request body
                expected.pop()

            assert actions == expected, "Unexpected request to ElasticSearch"

            response = {
                'items': [{
                    response_key: {
                        'status': 200
                    }
                }]
            }
            return (200, {}, json.dumps(response))

        if mock_elastic:
            self.requests_mock.add_callback(
                responses.POST,
                'https://example.com:443/_bulk',
                callback=es_callback,
                content_type='application/json'
            )

        index.handler(records, MockContext())

    def test_unsupported_contents(self):
        assert self._get_contents('foo.exe', '.exe') == ""
        assert self._get_contents('foo.exe.gz', '.exe.gz') == ""

    def test_get_plain_text(self):
        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(b'Hello World!\nThere is more to know.'),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.txt',
                'IfMatch': 'etag',
                'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
            }
        )

        contents = index.get_plain_text(
            'test-bucket',
            'foo.txt',
            compression=None,
            etag='etag',
            version_id=None,
            s3_client=self.s3_client,
            size=123
        )
        assert contents == "Hello World!\nThere is more to know."

    def test_text_contents(self):
        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(b'Hello World!'),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.txt',
                'IfMatch': 'etag',
                'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
            }
        )

        assert self._get_contents('foo.txt', '.txt') == "Hello World!"

    def test_gzipped_text_contents(self):
        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(compress(b'Hello World!')),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.txt.gz',
                'IfMatch': 'etag',
                'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
            }
        )

        assert self._get_contents('foo.txt.gz', '.txt.gz') == "Hello World!"

    def test_notebook_contents(self):
        notebook = (BASE_DIR / 'normal.ipynb').read_bytes()

        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(notebook),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.ipynb',
                'IfMatch': 'etag',
            }
        )

        assert "model.fit" in self._get_contents('foo.ipynb', '.ipynb')

    def test_gzipped_notebook_contents(self):
        notebook = compress((BASE_DIR / 'normal.ipynb').read_bytes())

        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(notebook),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.ipynb.gz',
                'IfMatch': 'etag',
            }
        )

        assert "Model results visualization" in self._get_contents('foo.ipynb.gz', '.ipynb.gz')

    def test_parquet_contents(self):
        parquet = (BASE_DIR / 'amazon-reviews-1000.snappy.parquet').read_bytes()
        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(parquet),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.parquet',
                'IfMatch': 'etag',
            }
        )

        contents = self._get_contents('foo.parquet', '.parquet')
        size = len(contents.encode('utf-8', 'ignore'))
        assert size <= index.ELASTIC_LIMIT_BYTES
        # spot check for contents
        assert "This is not even worth the money." in contents
        assert "As for results; I felt relief almost immediately." in contents
        assert "R2LO11IPLTDQDX" in contents

    # see PRE conditions in conftest.py
    @pytest.mark.extended
    def test_parquet_extended(self):
        directory = (BASE_DIR / 'amazon-reviews-pds')
        files = directory.glob('**/*.parquet')
        for f in files:
            print(f"Testing {f}")
            parquet = f.read_bytes()

            self.s3_stubber.add_response(
                method='get_object',
                service_response={
                    'Metadata': {},
                    'ContentLength': 123,
                    'Body': BytesIO(parquet),
                },
                expected_params={
                    'Bucket': 'test-bucket',
                    'Key': 'foo.parquet',
                    'IfMatch': 'etag',
                }
            )
