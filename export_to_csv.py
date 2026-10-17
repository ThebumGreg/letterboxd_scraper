import csv
import io
from database import get_aggregated_active_films
from logger import logger

def generate_consolidated_csv_buffer() -> io.StringIO:
    """
    Generates a single consolidated CSV buffer containing active films,
    user attribution, user counts, and direct Letterboxd URLs.
    """
    active_films = get_aggregated_active_films()
    output = io.StringIO()
    writer = csv.writer(output)

    # Header Row
    writer.writerow([
        "Movie Title",
        "Movie Slug",
        "Movie URL",
        "Total Users Added",
        "Added By Users",
        "Latest Seen At"
    ])

    for film in active_films:
        display_name = film["full_display_name"] or film["movie_title"]
        writer.writerow([
            display_name,
            film["movie_slug"],
            film["movie_url"],
            film["user_count"],
            film["added_by_users"],
            film["latest_seen"]
        ])

    output.seek(0)
    return output