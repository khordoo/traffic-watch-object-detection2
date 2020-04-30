import psycopg2
from datetime import datetime
import time
from collections import defaultdict
from dateutil import tz
from flask import Flask
from services import ImageAnalysisService, Database, HistoricalImagePoinst
from config import Config
import logging
import concurrent.futures as futures
import concurrent
import requests
import skimage.io as io

logging.basicConfig(format='%(asctime)-15s %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
app = Flask(__name__)
config = Config()
image_service = ImageAnalysisService()
database = Database(config)
historical_detections = HistoricalImagePoinst(database)
TARGET_LABELS = ['car', 'person', 'truck', 'bus', 'train', 'bicycle', 'motorbike', 'cat', 'dog']


class Postgres:
    def __init__(self):
        self.connect()
        self.cursor.execute('DELETE FROM camera')
        self.conn.commit()

    def connect(self):
        self.conn = psycopg2.connect(host="localhost", database="azure_ai", user="postgres", password="postgres")
        self.cursor = self.conn.cursor()

    def insert_camera(self, camera):
        self.cursor.execute(
            f"INSERT INTO camera (id,address,region,latitude,longitude) VALUES({camera['id']}, '{camera['address']}', '{camera['region']}', {camera['latitude']}, {camera['longitude']})")

        self.conn.commit()
        logger.info(f"Inserted camera: {camera['id']}")

    def insert(self, query):
        try:
            self.cursor.execute(query)
            self.conn.commit()
        except Exception as err:
            logger.error(f'Exception while trying to commit to the database:  {err}')
            self.connect()

    def multi_insert(self, queries):
        try:
            for query in queries:
                self.cursor.execute(query)
            self.conn.commit()
        except Exception as err:
            logger.error(f'Exception while trying to multi commit to the database:  {err}')
            self.connect()


class Scheduler:
    def __init__(self):
        self.database = Postgres()
        self.cameras = self.get_camera_locations()
        self.camera_ids = list(map(lambda camera: camera['id'], self.cameras))
        self.insert_cameras(self.cameras)
        self.session = requests.Session()
        self.detection_url = 'http://127.0.0.1:5000/analysis'
        self.FETCHING_INTERVAL_SEC = 600 #10 minutes

    def insert_cameras(self, cameras):
        for camera in cameras:
            self.database.insert_camera(camera)

    def insert_detections(self, detections):
        # insert into counts
        location_queries = []
        count_queries = []
        for detection in detections:
            confidences = detection['countsConfidence']
            # Creating counts queries
            for object_type, count in detection['counts'].items():
                if object_type not in TARGET_LABELS:
                    continue
                confidence = confidences[object_type]
                counts = f"INSERT INTO count (camera_id , time,label,count, confidence) VALUES({detection['cameraId']},'{detection['time']}','{object_type}',{count},{confidence})"
                count_queries.append(counts)

            # Creating individual objects queries
            for item in detection["detection"]:
                if item['label'] not in TARGET_LABELS:
                    continue
                x0 = item['topleft']['x']
                y0 = item['topleft']['y']
                x1 = item['bottomright']['x']
                y1 = item['bottomright']['y']
                xc = x0 + 0.5 * (x1 - x0)
                yc = y0 + 0.5 * (y1 - y0)
                query = f"INSERT INTO object_location (camera_id ,time  ,label  , x_top_left ,y_top_left  ,x_bottom_right , y_bottom_right ,x_center , y_center ,confidence  ) " \
                        f"VALUES ({detection['cameraId']},'{detection['time']}' , '{item['label']}',{x0},{y0} ,{x1} ,{y1} ,{xc},{yc},{item['confidence']} )"
                location_queries.append(query)

        if count_queries:
            self.database.multi_insert(count_queries)
            logger.info(f'Inserted {len(count_queries)} records into the count table')

        if location_queries:
            self.database.multi_insert(location_queries)
            logger.info(f'Inserted {len(location_queries)} records into objects_location table')

    def get_camera_locations(self):
        cameras = []
        response = requests.get('https://data.calgary.ca/api/views/6fv8-ymsc/rows.json')
        locations = response.json()['data']
        for location in locations:
            cameras.append({
                'address': location[8],
                'image_url': location[10][0],
                'id': int(location[10][0].split('/')[-1].split('.')[0][3:]),
                'region': location[9],
                'latitude': float(location[12]),
                'longitude': float(location[11]),
            })
        return cameras

    def run(self):
        while True:
            try:
                start_time = datetime.now()
                detections = self.detect_objects()
                self.insert_detections(detections)

                elapsed_time = (datetime.now() - start_time).total_seconds()
                waiting_time = self.FETCHING_INTERVAL_SEC - elapsed_time
                logger.info(f'Elapsed time: {elapsed_time}')
                logger.info(f'Waiting time: {waiting_time}')
                if waiting_time < 0 or elapsed_time > self.FETCHING_INTERVAL_SEC:
                    waiting_time = 0
                logger.info(f'Waiting for : {waiting_time} sec.')
                time.sleep(waiting_time)  # 5 minutes
            except Exception as err:
                logger.error(f'Exception happened :{err} , Time : {datetime.now().isoformat()}', exc_info=True)

    def detect_objects(self):
        images = self.async_fetch_images(self.camera_ids)
        logger.info(f'Performing object detection in Yolo')
        detections = []
        for bundle in images:
            camera_id, camera_image = bundle
            counts = defaultdict(int)
            confidence = defaultdict(float)

            payload = {
                "camera_id": camera_id,
                "image": camera_image
            }
            prediction = image_service.detect(payload)
            detection = prediction['detections']
            for item in detection:
                counts[item['label']] += 1
                confidence[item['label']] += float(item['confidence'])
            # Average confidence per group
            for key, value in confidence.items():
                confidence[key] = value / counts[key]
            utc_time = datetime.now(tz=tz.UTC)
            calgary_time = utc_time.astimezone(tz.gettz('America/Edmonton')).isoformat()
            detections.append(
                {"cameraId": camera_id, "detection": detection, "counts": counts, "countsConfidence": confidence,
                 "time": calgary_time})

        return detections

    def async_fetch_images(self, camera_ids):
        def get_image(camera_id):
            url = f'http://trafficcam.calgary.ca/loc{camera_id}.jpg'
            image = io.imread(url)
            return image

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            download_images = {executor.submit(get_image, camera_id): camera_id
                               for camera_id in camera_ids}
            camera_images = []
            s_time = time.time()
            for future in concurrent.futures.as_completed(download_images):
                camera_id = download_images[future]
                camera_images.append((camera_id, future.result()))
            duration = time.time() - s_time
            logger.info(f'Fetched {len(camera_ids)} camera images in {duration} seconds')
        return camera_images


Scheduler().run()
