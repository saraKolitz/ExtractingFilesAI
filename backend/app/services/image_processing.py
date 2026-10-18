import cv2
import numpy as np

# Printed labels on the form. The handwritten answer sits to the LEFT of each label (Hebrew is RTL).
ANCHORS = ["משפחה", "פרטי", "זהות"]
# Vision often keeps the trailing colon or other punctuation in the word ("משפחה:").
ANCHOR_STRIP = ":.,-–_ "

GAP_FROM_ANCHOR = 10
CROP_WIDTH = 300
Y_PADDING = 15
SCALE = 5


def extract_field_images(image_bytes: bytes, words_data: list[dict]) -> dict[str, bytes]:
    """Crop the handwritten answer next to each anchor label and enlarge it.

    words_data is the output of ocr_service.get_word_coordinates. Returns {anchor: PNG bytes}
    for each anchor found; the first occurrence of an anchor wins.
    """
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode image")
    height, width = image.shape[:2]

    crops: dict[str, bytes] = {}
    for item in words_data:
        anchor = item["word"].strip(ANCHOR_STRIP)
        if anchor not in ANCHORS or anchor in crops:
            continue
        xs = [v["x"] for v in item["vertices"]]
        ys = [v["y"] for v in item["vertices"]]
        min_x, min_y, max_y = min(xs), min(ys), max(ys)

        crop_x_end = min_x - GAP_FROM_ANCHOR
        crop_x_start = max(0, min_x - CROP_WIDTH)
        crop_y_start = max(0, min_y - Y_PADDING)
        crop_y_end = min(height, max_y + Y_PADDING)
        if crop_x_end <= crop_x_start or crop_y_end <= crop_y_start:
            continue  # anchor at the left edge of the page - nothing to crop

        crop = image[crop_y_start:crop_y_end, crop_x_start:min(width, crop_x_end)]
        enlarged = cv2.resize(crop, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
        ok, encoded = cv2.imencode(".png", enlarged)
        if ok:
            crops[anchor] = encoded.tobytes()
    return crops
